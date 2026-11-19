""""What can I cook?": rank recipes in the local catalogue by the ingredients I have.

Matching works on normalised names (catalogue.normalise: lower-case, singular,
no punctuation). Something I have matches a recipe ingredient when the two are
equal, or when every word of one appears as a whole word in the other:
"tomato" matches "cherry tomato" and "chicken breast" matches "chicken", but
"pea" does not match "peach" or "peanut".

Pantry staples are stricter (see is_pantry): "pepper" covers "pepper, black,
ground" but not "red pepper", so real vegetables still show as missing.
"""
import difflib
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


# Words that turn an ingredient into a different product: having chicken doesn't
# mean having chicken stock, and rice isn't rice flour. If either side has one of
# these that the other lacks, they don't match.
DERIVED = frozenset({
    "stock", "broth", "bouillon", "powder", "flour", "starch", "sauce", "paste", "ketchup", "oil", "vinegar",
    "syrup", "jam", "extract", "essence", "cube", "seasoning", "granule", "milk", "cream", "butter", "cake",
    "biscuit", "crisp", "liqueur", "wine", "noodle", "jelly", "spread", "dressing", "marinade",
})


def words_match(a: frozenset[str], b: frozenset[str]) -> bool:
    """True if all words of one side appear as whole words in the other, and
    neither side is a derived product the other isn't ("chicken" vs "chicken stock")."""
    if not a or not b or not (a <= b or b <= a):
        return False
    return (a & DERIVED) == (b & DERIVED)


def ingredient_matches(have: str, ingredient: str) -> bool:
    """Public helper: does `have` match recipe ingredient `ingredient`?"""
    return words_match(frozenset(catalogue.normalise(have).split()),
                       frozenset(catalogue.normalise(ingredient).split()))


# Words that don't change what a staple is: "pepper, black, ground" is still pepper,
# "cold water" is still water - but "red pepper" or "coconut water" are not.
PANTRY_DESCRIPTORS = frozenset({
    "black", "white", "ground", "freshly", "fresh", "fine", "coarse", "sea", "table", "cold", "warm", "hot",
    "lukewarm", "boiling", "tap", "iodised", "iodized", "refined", "granulated", "caster", "extra", "virgin",
    "coldpressed", "neutral", "flavourless", "mill", "crushed", "cracked", "from", "for", "some", "little", "bit", "more", "pinch",
})


def is_pantry(norm: str, pantry_terms: list[frozenset[str]]) -> bool:
    """A recipe ingredient is a pantry staple only if, ignoring harmless descriptors,
    it IS the staple (unlike the looser have-matching used for my ingredients)."""
    words = frozenset(norm.split())
    core = words - PANTRY_DESCRIPTORS
    return any(core == t or words == t for t in pantry_terms)


def _matches_any(norm: str, terms: list[frozenset[str]]) -> bool:
    words = frozenset(norm.split())
    return any(words_match(words, t) for t in terms)


def vocabulary() -> set[str]:
    with catalogue.db() as conn:
        return {w for (norm,) in conn.execute("SELECT DISTINCT norm FROM ingredients") for w in norm.split()}


def correct_spelling(have: list[str], vocab: set[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Fix words that appear in no recipe but closely resemble one that does
    ("brocolli" -> "broccoli"). Returns (corrected items, {original: corrected})."""
    vocab = vocabulary() if vocab is None else vocab
    if not vocab:
        return list(have), {}
    out, fixes = [], {}
    for item in have:
        words = catalogue.normalise(item).split()
        new_words = []
        for w in words:
            if w in vocab or len(w) < 4:
                new_words.append(w)
                continue
            close = difflib.get_close_matches(w, vocab, n=1, cutoff=0.8)
            new_words.append(close[0] if close else w)
        fixed = " ".join(new_words)
        if words and new_words != words:
            fixes[item] = fixed
            out.append(fixed)
        else:
            out.append(item)
    return out, fixes


def find_recipes(have: list[str], pantry: list[str] | None = None, max_missing: int | None = None,
                 limit: int | None = 30) -> list[dict]:
    """Rank catalogue recipes by how well `have` covers their ingredients.

    pantry: staples that never count as missing (None = MC_PANTRY_STAPLES, [] = none).
    max_missing: drop recipes missing more than this many ingredients (None = no limit).
    Ranking: most of my ingredients used, then fewest missing, then highest share
    of needed ingredients covered, then rating (highest first), then name.
    Each result also has mine_used: how many of `have` the recipe uses.
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
        used, missing, seen, used_norms = [], [], set(), []
        for name, norm in ings:
            if not norm or norm in seen or is_pantry(norm, pantry_terms):
                continue
            seen.add(norm)
            if _matches_any(norm, have_terms):
                used.append(name)
                used_norms.append(norm)
            else:
                missing.append(name)
        if not used:
            continue
        if max_missing is not None and len(missing) > max_missing:
            continue
        mine_used = sum(1 for t in have_terms if any(words_match(t, frozenset(n.split())) for n in used_norms))
        results.append({
            "id": rid, "name": recipe["name"], "thumbnail": recipe["thumbnail"],
            "duration": recipe["duration"], "rating": recipe["rating"], "servings": recipe["servings"],
            "used": used, "missing": missing, "needed_count": len(used) + len(missing),
            "mine_used": mine_used,
        })

    # Most of MY ingredients used first, then fewest missing, then share covered, rating, name.
    results.sort(key=lambda r: (-r["mine_used"], len(r["missing"]), -len(r["used"]) / r["needed_count"],
                                -(r["rating"] or 0), (r["name"] or "").lower(), r["id"]))
    return results[:limit] if limit is not None and limit > 0 else results
