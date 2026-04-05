import os
import json
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Agentic Search")

# mount static folder for frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

# API keys
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# request body
class SearchRequest(BaseModel):
    query: str

@app.get("/")
async def root():
    return FileResponse("static/index.html")

# ── Step 2: Search ──────────────────────────────────────────────
async def search_web(query: str) -> list[dict]:
    """Call Tavily Search API, return list of {url, title, snippet}"""
    body = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": 10,
        "search_depth": "advanced"  # advanced gives richer snippets
    }
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.tavily.com/search", json=body, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return [{"url": x["url"], "title": x["title"], "snippet": x.get("content", "")}
            for x in results]

# ── Step 3: Scrape ──────────────────────────────────────────────
async def scrape_page(url: str, snippet: str = "") -> str:
    """Use Jina Reader to get clean markdown, fall back to Tavily snippet"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://r.jina.ai/{url}", timeout=20)
        if r.status_code == 200 and len(r.text) > 300:
            return r.text
    except Exception:
        pass
    # fallback: use the snippet Tavily already gave us
    return snippet

# ── Step 4: LLM Call (Groq primary, OpenRouter fallback) ───────
async def call_llm(prompt: str) -> str:
    """Try Groq first, fall back to OpenRouter if it fails"""
    try:
        return await call_groq(prompt)
    except Exception:
        return await call_openrouter(prompt)

async def call_groq(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048
    }
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

async def call_openrouter(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8000",
        "X-Title": "Agentic Search"
    }
    body = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": [{"role": "user", "content": prompt}]
    }
    async with httpx.AsyncClient() as client:
        r = await client.post("https://openrouter.ai/api/v1/chat/completions",
                              headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ── Step 5: Page Classifier ─────────────────────────────────────
async def classify_page(text: str) -> str:
    """Returns: listicle | profile | news"""
    prompt = f"""Look at this page content (first 500 chars):
{text[:500]}

Reply with exactly one word: listicle, profile, or news
- listicle: page lists multiple companies/items
- profile: page is about one specific company/person
- news: article mentioning companies in passing"""
    result = await call_llm(prompt)
    result = result.strip().lower()
    if result not in ["listicle", "profile", "news"]:
        return "listicle"  # safe default
    return result

# ── Step 6: Schema Inference ────────────────────────────────────
async def infer_schema(query: str) -> list[str]:
    """Ask LLM what columns make sense for this query"""
    prompt = f"""For the search query: "{query}"
What are the 5 most useful attributes to extract for each result?
Reply with ONLY a JSON array of strings, nothing else.
Example: ["name", "founded", "funding", "location", "focus"]"""
    result = await call_llm(prompt)
    # strip markdown fences if present
    result = result.strip().replace("```json", "").replace("```", "").strip()
    try:
        schema = json.loads(result)
        return schema[:6]  # max 6 columns
    except Exception:
        return ["name", "description", "location", "website", "founded"]

# ── Step 7: Entity Extraction ───────────────────────────────────
async def extract_entities(text: str, query: str, schema: list[str], source_url: str, page_type: str) -> list[dict]:
    """Extract structured entities from page content based on schema"""

    context = f"[Page type: {page_type}. Query context: {query}]\n\n"
    content = text[:6000] if page_type == "listicle" else text[:4000]
    fields = ", ".join(schema)

    prompt = f"""{context}{content}

Extract ALL entities/companies/items from the text above that are relevant to: "{query}"
For each one, extract these fields: {fields}
Also add a "source" field with this URL: {source_url}

Important rules:
- Extract as many entities as you can find, even if some fields are missing
- Use null for missing fields but still include the entity
- Infer reasonable values from context if clearly implied
- Return ONLY a valid JSON array, no explanation, no markdown

[{{"name": "Example Corp", "field2": "value", "source": "{source_url}"}}]"""

    result = await call_llm(prompt)
    result = result.strip().replace("```json", "").replace("```", "").strip()
    # sometimes LLM wraps in extra text, try to find the JSON array
    if "[" in result and "]" in result:
        start = result.index("[")
        end = result.rindex("]") + 1
        result = result[start:end]
    try:
        entities = json.loads(result)
        return entities if isinstance(entities, list) else []
    except Exception:
        return []

# ── Step 8: Deduplicate ─────────────────────────────────────────
def deduplicate(entities: list[dict]) -> list[dict]:
    """Merge entities with same name, keeping all sources"""
    seen = {}
    for e in entities:
        name = str(e.get("name", "")).lower().strip()
        if not name or name == "null":
            continue
        if name not in seen:
            seen[name] = e
        else:
            # merge: fill missing fields, append sources
            for k, v in e.items():
                if k == "source":
                    existing = seen[name].get("source", "")
                    if v and v not in existing:
                        seen[name]["source"] = existing + " | " + v
                elif not seen[name].get(k) or seen[name][k] == "null":
                    seen[name][k] = v
    return list(seen.values())

# ── Main Endpoint ───────────────────────────────────────────────
@app.post("/search")
async def search(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    # 1. search
    results = await search_web(req.query)
    if not results:
        raise HTTPException(status_code=404, detail="No search results found")

    # 2. infer schema from query
    schema = await infer_schema(req.query)

    # 3. scrape + classify + extract (run top 7 pages concurrently)
    async def process_page(result):
        url = result["url"]
        snippet = result.get("snippet", "")
        text = await scrape_page(url, snippet)
        if not text or len(text) < 50:
            return []
        page_type = await classify_page(text)
        return await extract_entities(text, req.query, schema, url, page_type)

    tasks = [process_page(r) for r in results[:7]]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # flatten results, skip errors
    all_entities = []
    for r in all_results:
        if isinstance(r, list):
            all_entities.extend(r)

    # 4. deduplicate
    final = deduplicate(all_entities)

    return {
        "query": req.query,
        "schema": schema,
        "results": final,
        "total": len(final)
    }
