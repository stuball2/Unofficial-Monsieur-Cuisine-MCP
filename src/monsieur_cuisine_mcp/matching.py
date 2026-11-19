""""What can I cook?": rank recipes in the local catalogue by the ingredients I have.

Matching works on normalised names (catalogue.normalise: lower-case, singular,
no punctuation). Something I have matches a recipe ingredient when the two are
equal, or when every word of one appears as a whole word in the other:
"tomato" matches "cherry tomato" and "chicken breast" matches "chicken", but
"pea" does not match "peach" or "peanut".
"""
from collections.abc import Iterable

from . import catalogue
from .config import MC_PANTRY_STAPLES


def _terms(items: Iterable[str] | None) -> list[frozenset[str]]:
    """Normalise inputs to word sets, dropping blanks and duplicates."""
    out: list[frozenset[str]] = []
    for item in items or []:
        words = frozenset(catalogue.normalise(item).split())
        if words and words not in out:
            out.append(words)
    return out


def words_match(a: frozenset[str], b: frozenset[str]) -> bool:
    """True if all words of one side appear as whole words in the other."""
    return bool(a) and bool(b) and (a <= b or b <= a)


def ingredient_matches(have: str, ingredient: str) -> bool:
    """Public helper: does `have` match recipe ingredient `ingredient`?"""
    return words_match(frozenset(catalogue.normalise(have).split()),
                       frozenset(catalogue.normalise(ingredient).split()))


def _matches_any(norm: str, terms: list[frozenset[str]]) -> bool:
    words = frozenset(norm.split())
    return any(words_match(words, t) for t in terms)


def find_recipes(have: list[str], pantry: list[str] | None = None, max_missing: int | None = 3,
                 limit: int | None = 30) -> list[dict]:
    """Rank catalogue recipes by how well `have` covers their ingredients.

    pantry: staples that never count as missing (None = MC_PANTRY_STAPLES, [] = none).
    max_missing: drop recipes missing more than this many ingredients (None = no limit).
    Ranking: fewest missing, then highest share of needed ingredients covered,
    then rating (highest first), then name.
    """
    have_terms = _terms(have)
    if not have_terms:
        return []
    pantry_terms = _terms(MC_PANTRY_STAPLES if pantry is None else pantry)

    with catalogue.db() as conn:
        recipes = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, name, thumbnail, duration, rating, servings FROM recipes")}
        ingredients: dict[int, list[tuple[str, str]]] = {}
        for row in conn.execute("SELECT recipe_id, name, norm FROM ingredients ORDER BY recipe_id, position"):
            ingredients.setdefault(row["recipe_id"], []).append((row["name"], row["norm"]))

    results = []
    for rid, ings in ingredients.items():
        recipe = recipes.get(rid)
        if recipe is None:
            continue
        used, missing, seen = [], [], set()
        for name, norm in ings:
            if not norm or norm in seen or _matches_any(norm, pantry_terms):
                continue
            seen.add(norm)
            (used if _matches_any(norm, have_terms) else missing).append(name)
        if not used:
            continue
        if max_missing is not None and len(missing) > max_missing:
            continue
        results.append({
            "id": rid, "name": recipe["name"], "thumbnail": recipe["thumbnail"],
            "duration": recipe["duration"], "rating": recipe["rating"], "servings": recipe["servings"],
            "used": used, "missing": missing, "needed_count": len(used) + len(missing),
        })

    results.sort(key=lambda r: (len(r["missing"]), -len(r["used"]) / r["needed_count"],
                                -(r["rating"] or 0), (r["name"] or "").lower(), r["id"]))
    return results[:limit] if limit is not None and limit > 0 else results
