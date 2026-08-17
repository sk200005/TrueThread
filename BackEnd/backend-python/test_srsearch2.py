import requests
import urllib.parse

url = "https://en.wikipedia.org/w/api.php"
query = "1987 Jammu and Kashmir Legislative Assembly election"
params = {
    "action": "query",
    "list": "search",
    "srsearch": query,
    "format": "json",
    "srlimit": 5
}
headers = {"User-Agent": "SwayamsResearchApp/1.0"}
resp = requests.get(url, params=params, headers=headers)
data = resp.json()
print("Search results:")
for item in data.get("query", {}).get("search", []):
    print(f"- {item['title']}")
