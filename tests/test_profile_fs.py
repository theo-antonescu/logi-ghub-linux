from logi_ghub.features import profile_fs as pf

# bytes read from a G915 X (directory of bank 0 and its first profile)
DIRECTORY = bytes.fromhex("3ea1f833 03 01 81014063 ff67163e 08 02 81014063 65c92231 09 03 81014063 65c92231 0a ff".replace(" ", ""))
PROFILE = bytes.fromhex(
    "0001003c012c" + "0044006500660061007500 6c 0074 0020 0063 006f 2026 0000".replace(" ", "")
    + "0c00000000000020640000" + "00" * 11 + "0c00000000000020190000" + "00" * 11
    + "030000000000000000000000004400180 8ffffffffffffffff".replace(" ", "")
)
TWO_COLOUR = bytes.fromhex("ff00ab00b8fc0032 6400".replace(" ", ""))


def test_directory():
    assert pf.parse_directory(DIRECTORY) == [(1, 8, 0xFF67163E), (2, 9, 0x65C92231), (3, 10, 0x65C92231)]


def test_profile():
    p = pf.parse_profile(1, 8, PROFILE)
    assert p.name == "Default co…"
    assert (p.powersave_timeout, p.poweroff_timeout) == (60, 300)
    assert p.lighting.effect_id == 0x0C and p.lighting.brightness == 100
    assert p.powersave_lighting.brightness == 25


def test_two_colour_effect():
    e = pf.StoredEffect(0x18, TWO_COLOUR)
    assert e.colors == [0xFF00AB, 0x00B8FC] and e.brightness == 100
    assert e.to_lighting()["animation"]["colors"] == [0xFF00AB, 0x00B8FC]
