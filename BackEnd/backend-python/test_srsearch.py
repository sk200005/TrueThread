import requests

url = "https://en.wikipedia.org/w/api.php"
params = {
    "action": "query",
    "list": "search",
    "srsearch": "what happend in 1987 Jammu and Kashmir Legislative Assembly election",
    "format": "json",
    "srlimit": 5
}
headers = {"User-Agent": "SwayamsResearchApp/1.0"}
resp = requests.get(url, params=params, headers=headers)
data = resp.json()
print("Search results:")
for item in data.get("query", {}).get("search", []):
    print(f"- {item['title']}")
