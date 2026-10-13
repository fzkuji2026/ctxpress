import errno
import hashlib
import io
import pytest
from ctxpress.harness.jobs import plan


def test_hash_recovers_transient_read_that_advanced_offset(monkeypatch):
    content = bytes(range(256)) * 1000
    class Reader(io.BytesIO):
        failed = False
        def read(self, size=-1):
            assert size <= 65536
            if self.tell() >= 65536 and not self.failed:
                self.failed = True
                super().read(37)
                raise OSError(errno.ENOMEM, 'transient')
            return super().read(size)
    monkeypatch.setattr(plan, 'open', lambda *a, **k: Reader(content), raising=False)
    monkeypatch.setattr(plan.time, 'sleep', lambda _: None)
    assert plan.file_sha256('fixture.bin') == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize('code,expected_reads', [(errno.EIO, 1), (errno.ENOMEM, 6)])
def test_hash_propagates_permanent_or_exhausted_io_errors(monkeypatch, code, expected_reads):
    calls = []
    class Reader(io.BytesIO):
        def read(self, size=-1):
            calls.append(size)
            raise OSError(code, 'persistent')
    monkeypatch.setattr(plan, 'open', lambda *a, **k: Reader(b'data'), raising=False)
    monkeypatch.setattr(plan.time, 'sleep', lambda _: None)
    with pytest.raises(OSError) as exc:
        plan.file_sha256('fixture.bin')
    assert exc.value.errno == code and len(calls) == expected_reads


def test_hash_matches_real_file_and_rejects_credentials(tmp_path):
    p = tmp_path / 'input.bin';p.write_bytes(b'payload' * 20000)
    assert plan.file_sha256(p) == hashlib.sha256(p.read_bytes()).hexdigest()
    p = tmp_path / 'auth.json';p.write_text('{}')
    with pytest.raises(ValueError, match='credential'):
        plan.file_sha256(p)
