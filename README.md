# Agentic Search

Agentic Search is a system I built that takes any topic query and returns a structured, source-cited table of entities extracted live from the web using LLMs. For example, if you search "AI startups in New York" you get back a table of companies with their founding year, funding, location, and product description where every single row links back to the page it was pulled from.

---

## What It Does

The idea is simple. You type a query, the system searches the web, scrapes the top results, runs them through an LLM to pull out structured data, and renders everything as a clean table with citations. The columns in the table are not fixed either. They are inferred from the query itself, so a search about startups gives you funding and founders while a search about restaurants would give you ratings and neighborhoods instead.

---

## Tech Stack

| Component | Tool |
|---|---|
| Backend | FastAPI (Python) |
| Search | Tavily Search API |
| Scraping | Jina Reader API |
| LLM Primary | Groq running Llama 3.3 70B |
| LLM Fallback | OpenRouter running Llama 3.3 70B |
| Frontend | Plain HTML and JavaScript |

---

## How the Pipeline Works

### Search
The query goes to Tavily's Search API with advanced search depth enabled. I chose advanced over basic because it returns actual page content alongside the URLs, not just titles and meta descriptions. This extra content becomes really useful later as a fallback when scraping fails.

### Scraping
Each URL gets passed through Jina Reader which returns clean markdown by stripping out all the noise like navigation bars, ads, cookie banners and footers. The reason I used Jina instead of writing a raw HTML scraper is that raw HTML is mostly garbage and you would spend more time cleaning it than actually using it. Jina also handles JavaScript rendered pages without needing a headless browser which would have added a lot of complexity. If Jina comes back with thin or empty content for a page, the system falls back to using the snippet that Tavily already returned for that URL so we always have something to work with.

### Page Classification
Before sending anything to the LLM for extraction, each page gets classified as one of three types: a listicle that lists multiple entities, a profile focused on a single company or person, or a news article that mentions companies in passing. I added this step because the extraction prompt needs to be framed differently depending on what kind of page it is reading. A listicle with 20 companies needs a different approach than a deep profile page about one startup.

### Schema Inference
One of the things I wanted to avoid was a hardcoded schema. Instead, before any extraction happens, I send the query to the LLM and ask it what columns would be most useful for this kind of search. This single call at the start of the pipeline means the table adapts to whatever topic you search for rather than always showing the same generic columns.

### Entity Extraction
Each scraped page gets sent to the LLM along with the inferred schema and the page type as context. The LLM extracts every relevant entity it can find and returns them as a JSON array. I also added a JSON boundary parser that finds the array even if the LLM adds extra text around it, which happens sometimes with open source models.

### Deduplication
The same company can appear on multiple pages. Instead of showing it twice, the system merges entities with the same name into one row, fills in any missing fields from the secondary source, and keeps all the source URLs. That is why some rows in the table show source 1 and source 2.

### LLM Fallback
Groq is the primary LLM because it is very fast and has a generous free tier. If Groq hits a rate limit or fails for any reason, the system automatically retries the same prompt with OpenRouter. This means the pipeline keeps running even when one provider is having issues.

---

## Design Decisions and Trade-offs

The biggest decision was using Jina Reader for scraping instead of building something with Playwright or BeautifulSoup. Playwright would handle more edge cases but it adds significant deployment complexity and is much slower. Jina gets the job done for most pages and the Tavily fallback covers the gaps.

I chose Tavily over Brave or SerpAPI because Tavily is purpose built for AI agents. The advanced search depth returns real content per result which no other free search API does at the same quality level.

Running all page scraping and extraction concurrently with asyncio.gather was important for latency. Doing it sequentially across 7 pages would take close to a minute. Concurrently it runs in about 10 to 15 seconds.

The main trade-off I accepted is that Groq and OpenRouter both have free tier rate limits. Under heavy use you can hit a 429 error temporarily. For a production system I would use a paid LLM endpoint with proper rate limit handling and request queuing.

---

## Setup Instructions

The only things you need are Python 3.9 or higher and an internet connection. No GPU is required because all the heavy computation happens on external API servers.

Start by cloning the repository and moving into the folder.

```bash
git clone https://github.com/YOUR_USERNAME/agentic-search.git
cd agentic-search
```

Create a virtual environment and activate it.

```bash
python -m venv venv

source venv/bin/activate        # macOS and Linux
venv\Scripts\activate           # Windows
```

Install the dependencies.

```bash
pip install -r requirements.txt
```

Copy the environment template and fill in your API keys.

```bash
cp .env.example .env
```

Your .env file should look like this.

```
TAVILY_API_KEY=tvly-...
GROQ_API_KEY=gsk_...
OPENROUTER_API_KEY=sk-or-...
```

All three keys are completely free and require no credit card. You can get them at tavily.com, console.groq.com, and openrouter.ai respectively.

Finally, start the server.

```bash
uvicorn main:app --reload
```

Then open http://localhost:8000 in your browser and try a search.

---

## API

The backend exposes a single POST endpoint at /search that accepts a JSON body with a query field and returns the schema, results array, and total count.

```
POST /search
Content-Type: application/json

{ "query": "AI startups in healthcare" }
```

A typical response looks like this.

```json
{
  "query": "AI startups in healthcare",
  "schema": ["name", "founded", "funding", "location", "product_description"],
  "results": [
    {
      "name": "Abridge",
      "founded": "2018",
      "funding": "$212M",
      "location": "Pittsburgh, PA",
      "product_description": "AI for medical conversation documentation",
      "source": "https://..."
    }
  ],
  "total": 37
}
```

---

## Known Limitations

The free tier rate limits on Groq and OpenRouter mean that running many queries in quick succession can cause temporary failures. Jina Reader occasionally returns empty content for heavily JavaScript rendered pages though the Tavily fallback handles most of these cases. Deduplication works on exact name matching so slight variations like "OpenAI" versus "Open AI" can produce duplicate rows. Pages that do not have detailed information will produce rows with many null fields which is intentional since I preferred honest nulls over hallucinated values.

---

## File Structure

```
agentic-search/
├── main.py              # entire backend pipeline
├── requirements.txt     # dependencies
├── .env.example         # environment template
├── .gitignore
├── README.md
└── static/
    └── index.html       # frontend UI
```
