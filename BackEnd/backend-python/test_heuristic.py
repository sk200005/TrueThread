import requests

def extract_keywords(query: str) -> str:
    stopwords = {"what", "when", "where", "why", "who", "how", "is", "are", "do", "does", "did", "happen", "happened", "in", "on", "at", "to", "the", "a", "an", "of", "for", "about", "tell", "me", "explain"}
    words = query.split()
    # Also strip punctuation
    import re
    keywords = []
    for w in words:
        w_clean = re.sub(r'[^\w\s]', '', w)
        if w_clean.lower() not in stopwords and len(w_clean) > 0:
            keywords.append(w_clean)
    return " ".join(keywords)

original_query = "what happend in 1987 Jammu and Kashmir Legislative Assembly election"
kw_query = extract_keywords(original_query)
print(f"Original: {original_query}")
print(f"Keywords: {kw_query}")

url = "https://en.wikipedia.org/w/api.php"
params = {
    "action": "query",
    "list": "search",
    "srsearch": kw_query,
    "format": "json",
    "srlimit": 5
}
headers = {"User-Agent": "SwayamsResearchApp/1.0"}
resp = requests.get(url, params=params, headers=headers)
data = resp.json()
print("Search results:")
for item in data.get("query", {}).get("search", []):
    print(f"- {item['title']}")
