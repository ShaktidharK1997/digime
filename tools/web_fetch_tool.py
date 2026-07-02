"""Web fetch tool — URL fetcher with structured content extraction."""

import json
import re

import httpx
from bs4 import BeautifulSoup

FETCH_URL_SCHEMA = {
    "name": "fetch_url",
    "description": (
        "Fetch and extract structured data from any URL. Use for recipes, "
        "job postings, articles, or any web content Shakti shares."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch."},
            "extract": {
                "type": "string",
                "enum": ["recipe", "article", "job_posting", "auto"],
                "description": "Extraction mode. Defaults to 'auto'.",
            },
        },
        "required": ["url"],
    },
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 15


def _extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    """Extract JSON-LD structured data from the page."""
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return results


def _extract_recipe(soup: BeautifulSoup, json_ld: list[dict]) -> dict:
    """Extract recipe data from structured data or HTML."""
    # Try JSON-LD first
    for item in json_ld:
        if isinstance(item, dict):
            schema_type = item.get("@type", "")
            if schema_type == "Recipe" or (isinstance(schema_type, list) and "Recipe" in schema_type):
                ingredients = item.get("recipeIngredient", [])
                instructions = []
                raw_instructions = item.get("recipeInstructions", [])
                for step in raw_instructions:
                    if isinstance(step, str):
                        instructions.append(step)
                    elif isinstance(step, dict):
                        instructions.append(step.get("text", ""))

                return {
                    "type": "recipe",
                    "title": item.get("name", ""),
                    "ingredients": ingredients,
                    "instructions": instructions,
                    "prep_time": item.get("prepTime", ""),
                    "cook_time": item.get("cookTime", ""),
                    "servings": item.get("recipeYield", ""),
                }
            # Handle @graph
            if "@graph" in item:
                for node in item["@graph"]:
                    if isinstance(node, dict) and node.get("@type") == "Recipe":
                        return _extract_recipe(soup, [node])

    # Fallback: scan HTML for common recipe patterns
    title = soup.find("h1")
    title_text = title.get_text(strip=True) if title else ""

    ingredients = []
    for ul in soup.find_all(["ul", "ol"]):
        parent_class = " ".join(ul.get("class", []))
        if "ingredient" in parent_class.lower():
            for li in ul.find_all("li"):
                ingredients.append(li.get_text(strip=True))

    return {
        "type": "recipe",
        "title": title_text,
        "ingredients": ingredients,
        "instructions": [],
        "note": "Extracted from HTML — may be incomplete. Check the original page.",
    }


def _extract_article(soup: BeautifulSoup, json_ld: list[dict]) -> dict:
    """Extract article content."""
    # Try JSON-LD
    for item in json_ld:
        if isinstance(item, dict) and item.get("@type") in ("Article", "NewsArticle", "BlogPosting"):
            return {
                "type": "article",
                "title": item.get("headline", ""),
                "author": item.get("author", {}).get("name", "") if isinstance(item.get("author"), dict) else str(item.get("author", "")),
                "date": item.get("datePublished", ""),
                "content": item.get("articleBody", "")[:3000],
            }

    # Fallback: extract from HTML
    title = soup.find("h1")
    title_text = title.get_text(strip=True) if title else ""

    # Try common article containers
    article = soup.find("article") or soup.find("main") or soup.find("div", class_=re.compile(r"content|article|post"))
    if article:
        # Remove nav, sidebar, ads
        for tag in article.find_all(["nav", "aside", "footer", "script", "style"]):
            tag.decompose()
        content = article.get_text(separator="\n", strip=True)
    else:
        content = soup.get_text(separator="\n", strip=True)

    return {
        "type": "article",
        "title": title_text,
        "content": content[:3000],
    }


def _extract_job(soup: BeautifulSoup, json_ld: list[dict]) -> dict:
    """Extract job posting data."""
    for item in json_ld:
        if isinstance(item, dict) and item.get("@type") == "JobPosting":
            org = item.get("hiringOrganization", {})
            location = item.get("jobLocation", {})
            addr = location.get("address", {}) if isinstance(location, dict) else {}
            return {
                "type": "job_posting",
                "title": item.get("title", ""),
                "company": org.get("name", "") if isinstance(org, dict) else str(org),
                "location": addr.get("addressLocality", "") if isinstance(addr, dict) else "",
                "description": item.get("description", "")[:3000],
            }

    # Fallback
    title = soup.find("h1")
    content = soup.get_text(separator="\n", strip=True)

    return {
        "type": "job_posting",
        "title": title.get_text(strip=True) if title else "",
        "content": content[:3000],
        "note": "Extracted from HTML — may be incomplete.",
    }


def _auto_detect(json_ld: list[dict]) -> str:
    """Guess content type from JSON-LD."""
    for item in json_ld:
        if not isinstance(item, dict):
            continue
        schema_type = item.get("@type", "")
        if isinstance(schema_type, list):
            schema_type = " ".join(schema_type)
        schema_type_lower = schema_type.lower()
        if "recipe" in schema_type_lower:
            return "recipe"
        if any(t in schema_type_lower for t in ("article", "newsarticle", "blogposting")):
            return "article"
        if "jobposting" in schema_type_lower:
            return "job_posting"
        # Check @graph
        if "@graph" in item:
            for node in item.get("@graph", []):
                if isinstance(node, dict):
                    ntype = node.get("@type", "")
                    if isinstance(ntype, list):
                        ntype = " ".join(ntype)
                    if "recipe" in ntype.lower():
                        return "recipe"
    return "article"


def fetch_url(url: str, extract: str = "auto") -> str:
    """Fetch a URL and extract structured content."""
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code} fetching {url}"})
    except httpx.RequestError as e:
        return json.dumps({"error": f"Request failed: {e}"})

    soup = BeautifulSoup(resp.text, "html.parser")
    json_ld = _extract_json_ld(soup)

    if extract == "auto":
        extract = _auto_detect(json_ld)

    extractors = {
        "recipe": _extract_recipe,
        "article": _extract_article,
        "job_posting": _extract_job,
    }

    extractor = extractors.get(extract, _extract_article)
    result = extractor(soup, json_ld)
    result["source_url"] = url

    return json.dumps(result, default=str)
