"""Run the author's local SWE-Pruner weights on reconstructed published inputs.

No downloads or agent execution. Must run in the already installed model environment.
Checks output text, kept-token counts, and ctxpress's live adapter independently.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "repro"))
from swe_pruner_compare import author_functions, compare_case


def comparison_passed(counts, scope="published"):
    """Adapter agreement alone cannot certify a published-model reproduction."""
    n = counts.get("cases", 0)
    keys = ("adapter_same",) if scope == "adapter" else (
        "adapter_same", "text_same", "source_tokens_same", "kept_tokens_same", "model_input_tokens_same")
    return n > 0 and not counts.get("model_errors", 0) and all(counts.get(k) == n for k in keys)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--original", type=Path, default=ROOT.parent / "data/repro/swe-pruner")
    ap.add_argument("--inputs", type=Path)
    ap.add_argument("--output", type=Path, default=ROOT / "runs/repro/swe_pruner_model.jsonl")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--order", choices=("trajectory", "size"), default="trajectory")
    ap.add_argument("--attention", choices=("native", "sdpa"), default="native",
                    help="sdpa omits the fusion layer's unused attention weights; record this numerical backend change")
    ap.add_argument("--token-scores", action="store_true", help="include all per-token scores (large artifacts)")
    ap.add_argument("--acceptance", choices=("published", "adapter"), default="published",
                    help="default fails on any published text/count mismatch; adapter checks only integration")
    a = ap.parse_args(argv)
    # Both the weights and their backbone/tokenizer have already been downloaded.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(a.original / "swe-pruner/src"))
    from swe_pruner.prune_wrapper import SwePrunerForCodePruning, PruneRequest
    import torch
    model_dir = a.original / "swe-pruner/model"
    start = time.time()
    model = SwePrunerForCodePruning.from_pretrained(str(model_dir))
    model.eval()
    if a.attention == "sdpa":
        # The author's forward assigns attn_weights but never uses them. Asking
        # PyTorch not to return them enables its memory-efficient SDPA backend.
        # Threshold decisions can still vary numerically; equality is measured.
        def no_weights(module, args, kwargs):
            return args, dict(kwargs, need_weights=False)
        for module in model.modules():
            if isinstance(module, torch.nn.MultiheadAttention):
                module.register_forward_pre_hook(no_weights, with_kwargs=True)
    print(json.dumps(dict(loaded_seconds=round(time.time() - start, 2), cuda=torch.cuda.is_available())), flush=True)
    apply, prune, source_hashes = author_functions(a.original)
    for relative in ("swe-pruner/src/swe_pruner/prune_wrapper.py", "swe-pruner/src/swe_pruner/model_structure.py"):
        source_hashes[relative] = hashlib.sha256((a.original / relative).read_bytes()).hexdigest()
    paths = [model_dir / "config.json", model_dir / "model.safetensors"]
    weight_hashes = {}
    for path in paths:
        with path.open("rb") as stream:
            weight_hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    inputs = a.inputs or a.original / "verified/inputs.jsonl"
    rows = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines()]
    if a.order == "size":
        rows.sort(key=lambda row: len(row["text"]))
    if a.limit:
        rows = rows[:a.limit]
    a.output.parent.mkdir(parents=True, exist_ok=True)
    counts = dict(cases=0, text_same=0, counts_same=0, source_tokens_same=0, kept_tokens_same=0,
                  model_input_tokens_same=0, adapter_same=0, model_errors=0)
    metadata = dict(source_sha256=source_hashes, weights_sha256=weight_hashes,
                    inputs_sha256=hashlib.sha256(inputs.read_bytes()).hexdigest(),
                    python=sys.version, torch=torch.__version__, device=str(next(model.parameters()).device),
                    attention=a.attention, order=a.order, total=len(rows))
    a.output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    with a.output.open("w", encoding="utf-8") as out:
        for index, row in enumerate(rows):
            started = time.time()
            print(json.dumps(dict(starting=index + 1, total=len(rows), instance_id=row["instance_id"],
                                  chars=len(row["text"]), attention=a.attention)), flush=True)
            with torch.inference_mode():
                response = model.prune(PruneRequest(query=row["query"], code=row["text"], threshold=row["threshold"],
                                                   chunk_overlap_tokens=row["chunk_overlap_tokens"], always_keep_first_frags=False)).model_dump()
            # Adapter checker uses the same real model response on both paths.
            adapter = compare_case(dict(text=row["text"], query=row["query"], response=response), apply, prune)
            from ctxpress.methods.swepruner import render
            text, _ = render(row["text"], row["query"], lambda *args: response,
                             threshold=row["threshold"], chunk_overlap_tokens=row["chunk_overlap_tokens"])
            stats = row.get("expected_stats") or {}
            equal_counts = all(response[k] == stats.get(k) for k in ("origin_token_cnt", "left_token_cnt", "model_input_token_cnt"))
            result = dict(instance_id=row["instance_id"], trajectory=row["trajectory"], message=row["message"],
                          file=row["file"], source_sha256=row["source_sha256"], threshold=row["threshold"],
                          attention=a.attention,
                          text_same=text == row["expected"], counts_same=equal_counts,
                          source_tokens_same=response["origin_token_cnt"] == stats.get("origin_token_cnt"),
                          kept_tokens_same=response["left_token_cnt"] == stats.get("left_token_cnt"),
                          model_input_tokens_same=response["model_input_token_cnt"] == stats.get("model_input_token_cnt"),
                          adapter_same=adapter["same"] and adapter["calls_same"],
                          seconds=round(time.time() - started, 3),
                          response={k: v for k, v in response.items() if k != "token_scores" or a.token_scores}, expected_stats=stats)
            out.write(json.dumps(result, ensure_ascii=False) + "\n"); out.flush()
            counts["cases"] += 1
            for key in ("text_same", "counts_same", "source_tokens_same", "kept_tokens_same", "model_input_tokens_same", "adapter_same"):
                counts[key] += result[key]
            counts["model_errors"] += bool(response.get("error_msg"))
            print(json.dumps(dict(index=index + 1, **{k: result[k] for k in ("instance_id", "text_same", "counts_same", "seconds")})), flush=True)
    accepted = comparison_passed(counts, a.acceptance)
    report = dict(scope="published initial simple reads; excludes later modified files and unsupported shell commands",
                  acceptance=a.acceptance, accepted=accepted,
                  counts=counts, **metadata)
    a.output.with_suffix(".summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return int(not accepted)


if __name__ == "__main__":
    raise SystemExit(main())
