import zlib

from logi_ghub.features.profile_fs import DIR_SIZE, PROFILE_SIZE, ProfileFS
from logi_ghub.hidpp import features as F

# G HUB's own write of profile 2 (fixed green), recorded 2026-10-15
GREEN = bytes.fromhex(
    "00 01 00 3c 03 84 00 4e 00 65 00 77 00 20 00 50 00 72 00 6f 00 66 00 69 00 6c 00 65 00 00 01 86 ff 00 02 00 00 00 00 00 00 01 86 ff 00 02 00 00 00 00 "
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 44 00 18 08 ff ff ff ff ff ff ff ff")
GHUB_DIR = bytes.fromhex("63a24983030181014063ff67163e0802810140 63b6b4b44209038101406365c922310aff".replace(" ", ""))


class FakeKeyboard:
    def __init__(self, directory: bytes):
        self.files = {0: directory.ljust(DIR_SIZE, b"\xff")}
        self.calls = []
        self.open = None
        self.pending = None

    def call(self, feature, fn, *params):
        assert feature == F.PROFILE_MANAGEMENT
        raw = b"".join(bytes([p]) if isinstance(p, int) else bytes(p) for p in params)
        self.calls.append((fn, raw))
        if fn == 8:
            self.open = raw[1]
        elif fn == 12:
            off = (raw[0] << 8) | raw[1]
            return self.files[self.open][off : off + 16].ljust(16, b"\xff")
        elif fn == 2:
            self.pending = b""
        elif fn == 3:
            self.pending += raw
        elif fn == 7:
            size = (raw[2] << 8) | raw[3]
            data = self.pending[:size]
            assert zlib.crc32(data[4:] if raw[1] == 0 else data) == int.from_bytes(raw[4:8], "big")
            self.files[raw[1]] = data.ljust(DIR_SIZE, b"\xff") if raw[1] == 0 else data
        return bytes(16)


def test_writing_a_profile_matches_what_g_hub_sends():
    assert len(GREEN) == PROFILE_SIZE and zlib.crc32(GREEN) == 0xB6B4B442
    old_dir = bytes.fromhex("5ae3818c030181014063ff67163e08028101406310 51fa6d09038101406365c922310aff".replace(" ", ""))
    kb = FakeKeyboard(old_dir)
    ProfileFS(kb).write_profile(2, GREEN)
    assert kb.files[9] == GREEN
    assert kb.files[0][:36] == GHUB_DIR  # byte for byte what G HUB wrote
    writes = [c for c in kb.calls if c[0] in (2, 3, 7)]
    assert writes[0] == (2, bytes([0x00, 0x63, 0x00]))
    assert writes[8] == (7, bytes.fromhex("00090063b6b4b442"))
