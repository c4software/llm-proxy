"""Le champ model d'un formulaire multipart : lu pour router, réécrit pour
retirer le préfixe de backend, le reste du corps recopié tel quel."""

from llm_proxy import multipart as M

CT = "multipart/form-data; boundary=----b0undary"


def corps(model: bytes = b"bigchuck/qwen3-asr-1.7b", fichier: bytes = b"RIFF\x00\x01\r\n--x") -> bytes:
    return (b"------b0undary\r\n"
            b'Content-Disposition: form-data; name="model"\r\n\r\n' + model + b"\r\n"
            b"------b0undary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n" + fichier + b"\r\n"
            b"------b0undary--\r\n")


def test_is_multipart():
    assert M.is_multipart(CT)
    assert not M.is_multipart("application/json")
    assert not M.is_multipart("")


def test_model_field_read():
    assert M.model_field(corps(), CT) == "bigchuck/qwen3-asr-1.7b"


def test_model_field_absent_or_unparsable():
    sans = (b"------b0undary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n\r\n'
            b"x\r\n------b0undary--\r\n")
    assert M.model_field(sans, CT) is None
    assert M.model_field(corps(), "multipart/form-data") is None  # pas de boundary


def test_rewrite_keeps_everything_else():
    raw = corps()
    out = M.rewrite_model_field(raw, CT, "qwen3-asr-1.7b")
    assert M.model_field(out, CT) == "qwen3-asr-1.7b"
    assert out == corps(b"qwen3-asr-1.7b")
    # fichier binaire (CRLF et faux délimiteur compris) intact
    assert b"RIFF\x00\x01\r\n--x" in out


def test_model_after_file_and_quoted_boundary():
    raw = (b"--abc\r\n"
           b'Content-Disposition: form-data; name="image[]"; filename="p.png"\r\n\r\n'
           b"\x89PNG\r\n--abc-not-a-boundary\r\n"
           b"--abc\r\n"
           b'Content-Disposition: form-data; name="model"\r\n\r\n'
           b"bigchuck/Qwen-Image-2.1\r\n"
           b"--abc--\r\n")
    ct = 'multipart/form-data; boundary="abc"'
    assert M.model_field(raw, ct) == "bigchuck/Qwen-Image-2.1"
    out = M.rewrite_model_field(raw, ct, "Qwen-Image-2.1")
    assert M.model_field(out, ct) == "Qwen-Image-2.1"
    assert b"\x89PNG\r\n--abc-not-a-boundary" in out


def test_field_named_like_model_is_not_model():
    raw = (b"--abc\r\n"
           b'Content-Disposition: form-data; name="model_version"\r\n\r\nv2\r\n'
           b"--abc\r\n"
           b'Content-Disposition: form-data; name="model"\r\n\r\nbigchuck/x\r\n'
           b"--abc--\r\n")
    assert M.model_field(raw, "multipart/form-data; boundary=abc") == "bigchuck/x"
