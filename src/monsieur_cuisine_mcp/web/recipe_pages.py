"""Mobile-friendly recipe browser for the web interface.

    /recipes                       search (Monsieur Cuisine Smart only, default language)
    /recipes/{id}                  recipe detail with machine settings per step
    /recipes/{id}/favourite        POST add/remove favourite (by translationId)
    /favourites                    the logged-in user's bookmarked recipes

Everything that comes from the API is untrusted and goes through html.escape.
"""
import html
import logging
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .. import auth, recipes
from ..config import MC_API_DEFAULT_LANGUAGE, MC_IMAGE_HOST_REWRITES

logger = logging.getLogger(__name__)

DEVICE_TYPE = "MC3.0"
NAV = ('<nav class="row" style="justify-content:flex-start;margin-bottom:12px">'
       '<a class="btn" href="/">Status</a><a class="btn" href="/recipes">Recipes</a>'
       '<a class="btn" href="/favourites">Favourites</a></nav>')
RECIPE_CSS = """<style>
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.rcard{display:block;color:inherit;text-decoration:none;padding:0;overflow:hidden}
.rcard img,.hero{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--line)}
.hero{border-radius:10px;margin-bottom:12px}.rcard .in{padding:10px 12px}
.settings{font-weight:600;color:var(--acc);margin:4px 0}
input,select{font:inherit;padding:9px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.msg{padding:8px 12px}
</style>"""

_TAG_RE = re.compile(r"<[^>]+>")


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str) -> HTMLResponse:
    from .app import page  # late import: app imports this module
    return page(title, NAV + body, head=RECIPE_CSS)


def _error_card(message: str) -> str:
    return f'<div class="card bad msg">{esc(message)}</div>'


def strip_html(text: str | None) -> str:
    """HTML fragment -> plain text (tags removed, entities unescaped)."""
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _num(value) -> str:
    """Format a number without a pointless trailing .0."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def format_time(seconds) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


MODE_NAMES = {"customized": "Manual", "scale": "Weigh"}


def format_device_setting(ds: dict | None) -> str:
    """A step's deviceSetting -> e.g. "Manual · 90 °C · speed 1 · 30:00 · reverse".
    Returns "" for a text-only step."""
    if not ds:
        return ""
    parts: list[str] = []
    mode = ds.get("mode")
    if mode:
        parts.append(MODE_NAMES.get(mode, str(mode).replace("_", " ").title()))
    settings = ds.get("settings") or []
    s = settings[0] if settings and isinstance(settings[0], dict) else {}
    if s.get("temperature"):
        parts.append(f"{_num(s['temperature'])} °C")
    speed = s.get("speedFloat") if s.get("speedFloat") is not None else s.get("speed")
    if speed is not None:
        parts.append(f"speed {_num(speed)}")
    if s.get("time"):
        parts.append(format_time(s["time"]))
    if s.get("weight"):
        parts.append(f"{_num(s['weight'])} g")
    if ds.get("reverse"):
        parts.append("reverse")
    if ds.get("turbo"):
        parts.append("turbo")
    return " · ".join(parts)


def default_serving(recipe: dict) -> dict:
    sizes = recipe.get("servingSizes") or []
    for size in sizes:
        if size.get("id") == recipe.get("defaultServingSizeId"):
            return size
    return sizes[0] if sizes else {}


def _image(recipe: dict, *keys: str) -> str | None:
    for key in keys:
        url = (recipe.get(key) or {}).get("landscape")
        if url:
            return rewrite_image_host(url)
    return None


def rewrite_image_host(url: str) -> str:
    """Serve images from a host with a valid certificate (see MC_IMAGE_HOST_REWRITES)."""
    parts = urlsplit(url)
    target = MC_IMAGE_HOST_REWRITES.get(parts.hostname or "")
    return urlunsplit(parts._replace(netloc=target)) if target and parts.scheme == "https" else url


def _safe_url(url) -> str | None:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def _minutes(value) -> str:
    return f"{_num(value)} min" if value not in (None, "") else "–"


def _rating(recipe: dict) -> str:
    rating = recipe.get("rating")
    if rating in (None, ""):
        return ""
    count = recipe.get("totalRating")
    return f"★ {esc(_num(rating))}" + (f" ({esc(count)})" if count else "")


def _recipe_card(r: dict) -> str:
    img = _safe_url(_image(r, "thumbnail"))
    img_html = f'<img src="{esc(img)}" alt="" loading="lazy">' if img else ""
    return (f'<a class="card rcard" href="/recipes/{quote(str(r.get("id")))}">{img_html}<div class="in">'
            f'<strong>{esc(r.get("name"))}</strong><div class="muted">{esc(_minutes(r.get("duration")))}'
            f' {_rating(r)}</div></div></a>')


# ------------------------------------------------------------------ search
def _category_options(filters: dict) -> list[tuple[str, str]]:
    """Pull (id, name) category options out of get_recipe_filters' response.
    The option list key varies, so look for the usual ones."""
    groups = (filters.get("data") or {}).get("filters") or []
    for group in groups:
        if not isinstance(group, dict) or group.get("identifier") != "category":
            continue
        items = next((group[k] for k in ("options", "values", "items", "children")
                      if isinstance(group.get(k), list)), [])
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            oid = next((item[k] for k in ("id", "identifier", "value") if item.get(k) is not None), None)
            name = next((item[k] for k in ("name", "title", "label") if item.get(k)), oid)
            if oid is not None:
                out.append((str(oid), str(name)))
        return out
    return []


def _int_param(value: str | None, default: int | None = None, minimum: int = 1) -> int | None:
    try:
        return max(minimum, int(value)) if value not in (None, "") else default
    except ValueError:
        return default


async def recipes_search(request: Request) -> HTMLResponse:
    qp = request.query_params
    q = qp.get("q", "").strip()
    category = qp.get("category", "").strip()
    max_duration = _int_param(qp.get("max_duration"))
    page_no = _int_param(qp.get("page"), 1)

    try:
        categories = _category_options(await recipes.get_recipe_filters(language=MC_API_DEFAULT_LANGUAGE))
    except Exception:  # noqa: BLE001 - the search still works without the dropdown
        logger.warning("Could not load recipe filters", exc_info=True)
        categories = []

    cat_opts = '<option value="">Any category</option>' + "".join(
        f'<option value="{esc(cid)}"{" selected" if cid == category else ""}>{esc(name)}</option>'
        for cid, name in categories)
    form = f"""<form class="card" method="get" action="/recipes" style="display:block">
<div class="row"><input name="q" value="{esc(q)}" placeholder="Search recipes" style="flex:1 1 200px">
<select name="category">{cat_opts}</select>
<input name="max_duration" type="number" min="1" inputmode="numeric" placeholder="Max min" value="{esc(max_duration or "")}" style="width:100px">
<button>Search</button></div></form>"""

    try:
        data = await recipes.search_recipes(
            query=q or None, device_type=DEVICE_TYPE, categories=[category] if category else None,
            duration_max=max_duration, page=page_no, language=MC_API_DEFAULT_LANGUAGE)
    except Exception:  # noqa: BLE001
        logger.warning("Recipe search failed", exc_info=True)
        return _page("Recipes", form + _error_card("Search failed. The recipe service may be unavailable; try again shortly."))

    d = data.get("data") or {}
    results = [r for r in d.get("recipes") or [] if isinstance(r, dict)]
    current = _int_param(str(d.get("currentPage") or page_no), page_no)
    total_pages = _int_param(str(d.get("totalPage") or 1), 1)
    total = d.get("total", len(results))

    def link(p: int) -> str:
        params = {k: v for k, v in (("q", q), ("category", category), ("max_duration", max_duration or "")) if v}
        params["page"] = p
        return "/recipes?" + urlencode(params)

    paging = ""
    if total_pages > 1:
        prev = f'<a class="btn" href="{esc(link(current - 1))}">◀ Prev</a>' if current > 1 else "<span></span>"
        nxt = f'<a class="btn" href="{esc(link(current + 1))}">Next ▶</a>' if current < total_pages else "<span></span>"
        paging = f'<div class="row" style="margin-top:12px">{prev}<span class="muted">Page {current} of {total_pages}</span>{nxt}</div>'

    cards = "".join(_recipe_card(r) for r in results) or '<p class="muted">No recipes found.</p>'
    body = (form + f'<p class="muted">{esc(total)} recipe{"" if total == 1 else "s"} found</p>'
            f'<div class="cards">{cards}</div>{paging}')
    return _page("Recipes", body)


# ------------------------------------------------------------------ detail
def _ingredient_line(ing: dict) -> str:
    text = " ".join(str(p) for p in (ing.get("amount"), ing.get("unit"), ing.get("name")) if p not in (None, ""))
    if ing.get("description"):
        text += f", {ing['description']}"
    return f"<li>{esc(text)}</li>"


def _ingredients_html(serving: dict) -> str:
    ings = sorted((i for i in serving.get("ingredients") or [] if isinstance(i, dict)),
                  key=lambda i: i.get("order") or 0)
    groups = [g for g in serving.get("ingredientGroups") or [] if isinstance(g, dict)]
    if not groups:
        return f"<ul>{''.join(_ingredient_line(i) for i in ings)}</ul>"
    out, used = [], set()
    for g in groups:
        members = [i for i in ings if i.get("ingredientGroupId") == g.get("id")]
        used.update(id(i) for i in members)
        if members:
            out.append(f'<h3 style="font-size:.95rem;margin:8px 0 0">{esc(g.get("name"))}</h3>'
                       f"<ul>{''.join(_ingredient_line(i) for i in members)}</ul>")
    rest = [i for i in ings if id(i) not in used]
    if rest:
        out.append(f"<ul>{''.join(_ingredient_line(i) for i in rest)}</ul>")
    return "".join(out)


def _steps_html(serving: dict) -> str:
    steps = sorted((s for s in serving.get("steps") or [] if isinstance(s, dict)),
                   key=lambda s: s.get("order") or 0)
    items = []
    for s in steps:
        settings = format_device_setting(s.get("deviceSetting"))
        title = "<strong>" + esc(s.get("name")) + "</strong>" if s.get("name") else ""
        line = '<div class="settings">' + esc(settings) + "</div>" if settings else ""
        items.append(f'<li style="margin-bottom:10px">{title}{line}<div>{esc(s.get("description"))}</div></li>')
    return f"<ol>{''.join(items)}</ol>"


async def recipe_detail(request: Request) -> HTMLResponse:
    recipe_id = request.path_params["recipe_id"]
    try:
        data = await recipes.get_recipe(recipe_id, language=MC_API_DEFAULT_LANGUAGE)
    except Exception:  # noqa: BLE001
        logger.warning("Could not load recipe %s", recipe_id, exc_info=True)
        return _page("Recipe", _error_card("Could not load this recipe. Check the link, or try again shortly."))
    r = (data.get("data") or {}).get("recipe") or {}
    if not r:
        return _page("Recipe", _error_card("Recipe not found."))

    name = r.get("name") or r.get("title") or "Recipe"
    serving = default_serving(r)
    img = _safe_url(_image(r, "detailsImage", "thumbnail"))
    servings = " ".join(str(p) for p in (serving.get("amount"), serving.get("servingUnit")) if p not in (None, ""))
    cats = ", ".join(str(c.get("name")) for c in r.get("categories") or [] if isinstance(c, dict) and c.get("name"))
    rows = [("Preparation", _minutes(r.get("preparationDuration") or serving.get("preparationDuration"))),
            ("Total time", _minutes(r.get("duration") or serving.get("duration"))),
            ("Servings", servings or "–")]
    if cats:
        rows.append(("Categories", cats))
    table = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows)
    rating = _rating(r)
    if rating:
        table += f"<tr><td>Rating</td><td>{rating}</td></tr>"

    msg = request.query_params.get("msg")
    msg_html = f'<div class="card msg">{esc(msg)}</div>' if msg else ""
    fav = ""
    if r.get("translationId") is not None:
        action = esc(f"/recipes/{quote(str(recipe_id))}/favourite")
        tid = f'<input type="hidden" name="translation_id" value="{esc(r["translationId"])}">'
        fav = (f'<div class="row" style="justify-content:flex-start;margin-bottom:12px">'
               f'<form method="post" action="{action}">{tid}<input type="hidden" name="action" value="add">'
               f'<button>Add to favourites</button></form>'
               f'<form method="post" action="{action}">{tid}<input type="hidden" name="action" value="remove">'
               f'<button class="alt">Remove from favourites</button></form></div>')
    hints = strip_html(r.get("hintsAndTips"))
    url = _safe_url(r.get("url"))

    body = f"""{msg_html}{f'<img class="hero" src="{esc(img)}" alt="">' if img else ""}
<h2 style="font-size:1.15rem">{esc(name)}</h2>{fav}
<div class="card"><table>{table}</table></div>
{f'<div class="card"><p style="margin:0">{esc(r.get("description"))}</p></div>' if r.get("description") else ""}
{f'<div class="card"><h2>Hints &amp; tips</h2><p style="margin:0;white-space:pre-line">{esc(hints)}</p></div>' if hints else ""}
<div class="card"><h2>Ingredients</h2>{_ingredients_html(serving)}</div>
<div class="card"><h2>Steps</h2>{_steps_html(serving)}</div>
{f'<p><a href="{esc(url)}" target="_blank" rel="noopener">View on monsieur-cuisine.com</a></p>' if url else ""}"""
    return _page(str(name), body)


def _favourite_error(exc: Exception) -> str:
    if isinstance(exc, auth.NotLoggedInError):
        return "Not logged in. Log in from the status page first."
    if isinstance(exc, recipes.MCRequestError):
        return "The Monsieur Cuisine session was rejected. Log in again from the status page."
    return "Something went wrong talking to Monsieur Cuisine. Try again shortly."


async def recipe_favourite(request: Request) -> RedirectResponse:
    recipe_id = request.path_params["recipe_id"]
    # Plain urlencoded form; parsed by hand so python-multipart isn't required.
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    action = form.get("action")
    back = f"/recipes/{quote(str(recipe_id))}"
    try:
        raw = str(form.get("translation_id") or "").strip()
        translation_id = int(raw) if raw.isdigit() else None
        if translation_id is None:  # no/invalid hidden field: look it up
            data = await recipes.get_recipe(recipe_id, language=MC_API_DEFAULT_LANGUAGE)
            translation_id = ((data.get("data") or {}).get("recipe") or {}).get("translationId")
        if translation_id is None:
            msg = "This recipe can't be added to favourites."
        elif action == "remove":
            await recipes.remove_favorite_recipe(int(translation_id), language=MC_API_DEFAULT_LANGUAGE)
            msg = "Removed from favourites."
        else:
            await recipes.add_favorite_recipe(int(translation_id), language=MC_API_DEFAULT_LANGUAGE)
            msg = "Added to favourites."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Favourite %s for recipe %s failed", action, recipe_id, exc_info=True)
        msg = _favourite_error(exc)
    return RedirectResponse(f"{back}?{urlencode({'msg': msg})}", status_code=303)


# ------------------------------------------------------------------ favourites
def _bookmark_recipe(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    if item.get("id") is not None and item.get("name"):
        return item
    inner = item.get("recipe")
    if isinstance(inner, dict) and inner.get("id") is not None:
        return inner
    return None


async def favourites(request: Request) -> HTMLResponse:
    try:
        data = await recipes.list_favorite_recipes(language=MC_API_DEFAULT_LANGUAGE)
    except (auth.NotLoggedInError, recipes.MCRequestError) as exc:
        logger.info("Favourites unavailable: %s", exc)
        return _page("Favourites", f'<div class="card msg">{esc(_favourite_error(exc))} '
                                   '<a href="/">Go to the status page to log in</a>.</div>')
    except Exception:  # noqa: BLE001
        logger.warning("Could not load favourites", exc_info=True)
        return _page("Favourites", _error_card("Could not load favourites. Try again shortly."))
    d = data.get("data") or {}
    items = [r for r in (_bookmark_recipe(b) for b in d.get("bookmarks") or []) if r]
    total = d.get("total", len(items))
    cards = "".join(_recipe_card(r) for r in items) or '<p class="muted">No favourites yet.</p>'
    return _page("Favourites", f'<h2>Favourites</h2><p class="muted">{esc(total)} saved</p><div class="cards">{cards}</div>')
