import httpx
import pytest

from monsieur_cuisine_mcp import recipes
from monsieur_cuisine_mcp.config import MC_API_BASE_URL, MC_PROXY_API_URL
from monsieur_cuisine_mcp.recipes.search import _build_search_params


def test_search_params_encode_filters_and_sort():
    params = _build_search_params(
        query="soup", device_type="MC3.0", categories=["1", "2"], ingredient_categories=None,
        complexity=None, duration_min=10, duration_max=None, rating_min=None, rating_max=None,
        media=None, source="official", calories_min=None, calories_max=500, protein_min=None,
        protein_max=None, carbohydrate_min=None, carbohydrate_max=None, fat_min=None, fat_max=None,
        sort_field="rating", sort_direction="DESC")
    assert params.get_list("filters[category][]") == ["1", "2"]
    assert params["q"] == "soup" and params["filters[duration][from]"] == "10"
    assert params["filters[nutrientsCalories][to]"] == "500"
    assert params["sortBy[0][field]"] == "rating"


async def test_search_recipes_uses_public_api(mock_http, load_fixture):
    route = mock_http.get(f"{MC_API_BASE_URL}/api/v1/recipes/search/page/2").respond(
        json=load_fixture("mc-online-page.json"))
    data = await recipes.search_recipes(query="soupe", page=2, language="fr-FR")
    assert route.called
    req = route.calls.last.request
    assert req.url.params["q"] == "soupe" and req.headers["Accept-Language"] == "fr-FR"
    assert data["data"]["total"] == 3


async def test_search_recipes_rejects_unsupported_device():
    with pytest.raises(ValueError, match="device_type"):
        await recipes.search_recipes(device_type="MC2.0")


async def test_get_recipe_public(mock_http, load_fixture):
    mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/900001").respond(json=load_fixture("mc-online-recipe.json"))
    data = await recipes.get_recipe("900001")
    assert data["data"]["recipe"]["id"] == 900001


async def test_get_recipe_falls_back_to_own_recipe_when_unpublished(mock_http, logged_in):
    mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/42").respond(409, json={"message": "Recipe is not published"})
    proxy = mock_http.post(MC_PROXY_API_URL).respond(json={"data": {"recipe": {"id": 42, "title": "Mine"}}})
    data = await recipes.get_recipe("42")
    assert data["data"]["recipe"]["title"] == "Mine"
    sent = proxy.calls.last.request
    assert b"api/v3/auth/user/recipes/42" in sent.content
    assert "wordpress_logged_in_test=fake-session" in sent.headers["cookie"]


async def test_get_recipe_unpublished_and_logged_out_gives_clear_error(mock_http, logged_out):
    mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/42").respond(409)
    with pytest.raises(recipes.MCRequestError, match="log in first"):
        await recipes.get_recipe("42")


@pytest.mark.parametrize("status,body,match", [
    (403, {"code": 40008, "message": "Invalid category"}, r"Invalid category \(code 40008\)"),
    (403, None, "session expired"),
    (429, None, "rate-limited"),
    (502, None, "transient"),
])
async def test_proxy_errors_are_actionable(mock_http, logged_in, status, body, match):
    resp = httpx.Response(status, json=body) if body else httpx.Response(status, text="<html>")
    mock_http.post(MC_PROXY_API_URL).mock(return_value=resp)
    with pytest.raises(recipes.MCRequestError, match=match):
        await recipes.list_favorite_recipes()


async def test_public_calls_request_ingredient_names(mock_http, load_fixture):
    route = mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/800001").respond(json=load_fixture("mc3-en-recipe.json"))
    data = await recipes.get_recipe("800001")
    req = route.calls.last.request
    assert req.headers["device-type"] == "web" and req.headers["Accept-Language"] == "en-US"
    names = [i["name"] for i in data["data"]["recipe"]["servingSizes"][0]["ingredients"]]
    assert names[:2] == ["Milk", "risotto rice"]
