from monsieur_cuisine_mcp.recipes.nutrients import _build_nutrients, _merge_nutrients
from monsieur_cuisine_mcp.recipes.payloads import _recipe_to_put_payload


def test_build_nutrients_rounds_and_skips_none():
    assert _build_nutrients(449.6, 40.2, None, 15) == [
        {"name": "calories", "amount": 450, "unit": "kCal"},
        {"name": "protein", "amount": 40, "unit": "g"},
        {"name": "fat", "amount": 15, "unit": "g"},
    ]


def test_merge_nutrients_overlays_only_given_values():
    existing = [{"name": "calories", "amount": 300, "unit": "kCal"}, {"name": "fat", "amount": 9, "unit": "g"}]
    merged = {n["name"]: n["amount"] for n in _merge_nutrients(existing, None, 20.4, None, None)}
    assert merged == {"calories": 300, "fat": 9, "protein": 20}


def test_recipe_to_put_payload_round_trips_ids_and_servings():
    recipe = {
        "status": "draft", "languageLocale": "en-GB", "title": "Soup",
        "deviceTypes": [{"id": 13}], "complexity": {"id": 2}, "categories": [{"id": 7}, {}],
        "nutrients": [{"name": "calories", "amount": 200, "unit": "kCal", "extra": 1}],
        "thumbnail": {"landscapeMedia": {"id": 5}, "portraitMedia": {}},
        "servingSizes": [{"amount": 4, "unit": "portions", "steps": [{"title": "S1", "description": "d",
                          "mode": {"type": "turbo"}, "id": 99}],
                          "ingredientGroups": [{"name": "Main", "ingredients": [
                              {"name": "Leek", "amount": 1.5, "unit": "pc", "systemIngredientId": 0,
                               "isOptional": True}]}]}],
    }
    p = _recipe_to_put_payload(recipe)
    assert p["deviceTypeIds"] == [13] and p["complexityId"] == 2 and p["categoryIds"] == [7]
    assert p["thumbnail"] == {"portraitMediaId": None, "landscapeMediaId": 5}
    assert p["nutrients"] == [{"name": "calories", "amount": 200, "unit": "kCal"}]
    step = p["servingSizes"][0]["steps"][0]
    assert step == {"title": "S1", "description": "d", "mode": {"type": "turbo"}}
    ing = p["servingSizes"][0]["ingredientGroups"][0]["ingredients"][0]
    assert ing == {"name": "Leek", "amount": "1.5", "unit": "pc", "order": 0, "isOptional": True}
