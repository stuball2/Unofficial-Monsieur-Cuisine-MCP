import pytest

from monsieur_cuisine_mcp.recipes import devices as d


def test_customized_step_mode_maps_fields_and_rotation():
    mode = d._build_step_mode({"program": "customized", "temperature_celsius": 100,
                               "speed": 2, "time_seconds": 300, "reverse": True})
    assert mode == {"type": "customized", "modeSetting": None, "deviceSettings": [
        {"order": 0, "time": 300, "speed": 2, "temperature": 100, "clockwise": False}]}


def test_camel_case_program_is_normalized():
    assert d._build_step_mode({"program": "sousVide", "temperature_celsius": 60,
                               "time_seconds": 3600})["type"] == "sous_vide"


def test_unknown_program_rejected():
    with pytest.raises(ValueError, match="Unknown device program"):
        d._build_step_mode({"program": "teleport", "time_seconds": 1})


def test_program_without_settings_rejected():
    with pytest.raises(ValueError, match="needs at least one setting"):
        d._build_step_mode({"program": "steaming"})


def test_no_program_means_text_only_step():
    assert d._build_step_mode({"text": "Chop onions"}) is None


@pytest.mark.parametrize("texture", ["waxySoft", "waxy_soft"])
def test_cooking_eggs_derives_time_and_normalizes_texture(texture):
    mode = d._build_step_mode({"program": "cooking_eggs", "egg_size": "large", "egg_texture": texture})
    assert mode["modeSetting"] == {"size": "large", "texture": "waxy_soft"}
    assert mode["deviceSettings"][0]["time"] == 13 * 60


def test_precleaning_rejects_bad_duration():
    with pytest.raises(ValueError, match="cleaning_duration"):
        d._build_step_mode({"program": "precleaning", "cleaning_duration": "forever"})


def test_build_api_steps_fills_localized_default_titles():
    steps = d._build_api_steps(["Wash rice", {"title": "Cook", "text": "Boil",
                                               "program": "rice_cooking", "time_seconds": 900}], "pl-PL")
    assert steps[0] == {"title": "Krok 1", "description": "Wash rice"}
    assert steps[1]["title"] == "Cook" and steps[1]["mode"]["type"] == "rice_cooking"
    assert d._default_step_title("xx-XX", 3) == "Step 3"
