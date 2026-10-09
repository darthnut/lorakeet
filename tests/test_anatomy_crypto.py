"""Packet anatomy's encryption layer needs the cryptography package: a fresh install without it showed
"No module named 'cryptography'" on every packet (it was never in requirements.txt)."""
import anatomy


def test_aes_ctr_matches_the_nist_test_vector():
    # NIST SP 800-38A, F.5.1 CTR-AES128.Encrypt, first block
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    counter = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    assert anatomy._ctr(key, counter, plain).hex() == "874d6191b620e3261bef6864990db6ce"
