from dataclasses import dataclass
import hashlib
import yaml
from ctxpress.benchmarks.milestone.source_policy import bind
from ctxpress.benchmarks.milestone.adapter import SWEMilestone

@dataclass(frozen=True)
class Repo:
    raw_bytes: bytes
    config: dict
    source_path: str = 'original.yaml'
    @property
    def sha256(self):
        return hashlib.sha256(self.raw_bytes).hexdigest()


def test_every_trial_grades_the_agent_source_tree_with_author_repository_lists():
    config = dict(repo_src_dirs=['db'], test_dirs=['tests/**'], exclude=['examples/**'])
    original = Repo(yaml.safe_dump(config).encode(), config)
    metadata = dict(prune_extensions=['.go'], prune_keep_list=['core/mock.go'], residue_prune=False)
    bound = bind(original, metadata)
    assert bound.config == dict(config, residue_prune=True, residue_prune_scope='reference-added',
                                prune_extensions=['.go'], prune_keep_list=['core/mock.go'])
    assert yaml.safe_load(bound.raw_bytes) == bound.config and bound.sha256 != original.sha256
    assert original.config == config and bind(original, metadata).sha256 == bound.sha256


def test_repository_config_lists_win_and_absent_lists_use_author_defaults():
    config = dict(repo_src_dirs=['src'], test_dirs=['tests/**'], prune_extensions=['.rs'])
    bound = bind(Repo(yaml.safe_dump(config).encode(), config), dict(prune_extensions=['.go']))
    assert bound.config['prune_extensions'] == ['.rs'] and 'prune_keep_list' not in bound.config
    assert 'prune_extensions' not in bind(Repo(b'{}', {}), {}).config


def test_no_per_run_source_policy_switch():
    assert SWEMilestone().run_fields() == {'grading_workers'}
