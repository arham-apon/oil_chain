import urllib.request
import json
import sys

BASE_URL = "http://localhost:8000"

ENDPOINTS = {
    "health": "/v1/health",
    "instance": "/v1/instance",
    "regions": "/v1/regions",
    "depots": "/v1/depots",
    "stations": "/v1/stations",
    "routes": "/v1/routes",
    "supply_arrivals": "/v1/supply-arrivals",
    "events": "/v1/events",
    "metrics": "/v1/metrics",
    "allocations": "/v1/allocations"
}

def fetch_json(endpoint: str):
    url = f"{BASE_URL}{endpoint}"
    req = urllib.request.Request(url, headers={"User-Agent": "OilChain-Client/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}

def main():
    save_file = "--save" in sys.argv
    world_state = {}

    print("Fetching world state from simulator...")
    for name, ep in ENDPOINTS.items():
        data = fetch_json(ep)
        world_state[name] = data

    if save_file:
        with open("world_state.json", "w", encoding="utf-8") as f:
            json.dump(world_state, f, indent=2)
        print("World state successfully saved to world_state.json")
    else:
        print("\n--- SIMULATION INSTANCE ---")
        print(json.dumps(world_state.get("instance"), indent=2))

        print("\n--- DEPOTS ---")
        print(json.dumps(world_state.get("depots"), indent=2))

        print("\n--- STATIONS ---")
        print(json.dumps(world_state.get("stations"), indent=2))

        print("\n--- ROUTES ---")
        print(json.dumps(world_state.get("routes"), indent=2))

        print("\n--- METRICS ---")
        print(json.dumps(world_state.get("metrics"), indent=2))

        print("\nTip: Run `python fetch_world_state.py --save` to export full state to world_state.json")

if __name__ == "__main__":
    main()
