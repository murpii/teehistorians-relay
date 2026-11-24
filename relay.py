import json
import os
import sys
import time
import urllib.error
import urllib.request

WORLDSTATE_URL = "https://api.warframe.com/cdn/worldState.php"
MANIFEST_URL = "https://raw.githubusercontent.com/calamity-inc/wf.browse.oracle/senpai/data/"
ORACLE_URL = "https://oracle.browse.wf/bounty-cycle"
MASK = (1 << 64) - 1


def fetch_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "teehistorians-relay"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def fetch_worldstate(tries=3, wait=5):
    for attempt in range(tries):
        if attempt:
            time.sleep(wait * 2 ** (attempt - 1))
        try:
            body = fetch_text(WORLDSTATE_URL)
            if "WorldSeed" in body:
                return json.loads(body)
            failure = ValueError(f"The answer has no WorldSeed, it starts with {body[:200]!r}")
        except OSError as error:
            failure = error
    raise failure


def trim(world):
    kept_keys = ("Time", "ActiveMissions", "VoidStorms", "SyndicateMissions", "Sorties", "LiteSorties",
                 "EndlessXpSchedule", "Conquests", "VoidTraders", "Invasions", "Alerts", "Goals", "SeasonInfo")
    trimmed = {key: world[key] for key in kept_keys if key in world}
    if "SyndicateMissions" in trimmed:
        trimmed["SyndicateMissions"] = [entry for entry in trimmed["SyndicateMissions"]
                                        if entry["Tag"] in ("CetusSyndicate", "EntratiSyndicate")]
    return trimmed


class Rng:
    def __init__(self, seed):
        self.state = seed

    def pick(self, low, high):
        spread = high - low
        if spread != 0:
            self.state = (0x5851F42D4C957F2D * self.state + 0x14057B7EF767814F) & MASK
            low += ((self.state >> 32) & 0x3FFFFFFF) % (spread + 1)
        return low

    def shuffle(self, items):
        size = len(items)
        while size >= 2:
            index = self.pick(1, size) - 1
            items[index], items[size - 1] = items[size - 1], items[index]
            size -= 1


def base_name(path):
    name = path.split("/")[-1]
    for word in ("Easy", "VeryHard", "Hard", "Challenge"):
        found = name.find(word)
        if found != -1:
            return name[:found]
    return name


def entry_fits(entry, node, faction):
    return (entry["Node"] == "" or entry["Node"] == node) and (
        entry["CheckFaction"] == 0 or entry.get("Faction") == faction
    )


def compute_bounties(manifest, seed, faction, allies):
    rng = Rng(seed)
    nodes = list(manifest["ChallengeMissionNodes"])
    rng.shuffle(nodes)
    late_nodes = ["SolNode852", "SolNode850", "SolNode854"] if allies else list(nodes)
    ally_prefix = "/Lotus/Types/Gameplay/1999Wf/ProtoframeAllies/"
    banned_ally = {
        "SolNode856": "ArthurAllyAgent",
        "SolNode852": "LettieAllyAgent",
        "SolNode851": "AmirAllyAgent",
        "SolNode850": "EleanorAllyAgent",
        "SolNode853": "AoiAllyAgent",
        "SolNode854": "QuincyAllyAgent",
    }
    if allies:
        if 0 < len(allies) < len(nodes):
            drop_order = ["SolNode851", "SolNode852", "SolNode853", "SolNode854"]
            rng.shuffle(drop_order)
            while len(allies) < len(nodes) and drop_order:
                if drop_order[0] in nodes:
                    nodes.remove(drop_order[0])
                drop_order.pop(0)
        rng.shuffle(allies)
        for index in range(min(len(allies), len(nodes))):
            if allies[index] == ally_prefix + banned_ally.get(nodes[index], ""):
                other = (index + 1) % len(allies)
                allies[index], allies[other] = allies[other], allies[index]
    bounties = []
    used = set()
    for number, tier in enumerate(manifest["ChallengeMissionDifficultyTiers"], start=1):
        bounty = {}
        if number <= len(nodes):
            bounty["node"] = nodes[number - 1]
            if allies:
                bounty["ally"] = allies[number - 1]
        else:
            bounty["node"] = late_nodes[rng.pick(1, len(late_nodes)) - 1]
        fresh = []
        repeated = []
        for entry in tier["challenges"]:
            if entry_fits(entry, bounty["node"], faction):
                for challenge in entry["challenges"]:
                    if base_name(challenge) in used:
                        repeated.append(challenge)
                    else:
                        fresh.append(challenge)
        choices = fresh or repeated
        bounty["challenge"] = choices[rng.pick(1, len(choices)) - 1]
        used.add(base_name(bounty["challenge"]))
        bounties.append(bounty)
    return bounties


def fetch_manifests(base_url=MANIFEST_URL):
    files = {
        "ZarimanSyndicate": "ZarimanJobManifest.json",
        "EntratiLabSyndicate": "EntratiLabsJobManifest.json",
        "HexSyndicate": "WF1999JobManifest.json",
    }
    return {tag: json.loads(fetch_text(base_url + name))["data"] for tag, name in files.items()}


def hex_allies():
    names = ("AmirAllyAgent", "AoiAllyAgent", "ArthurAllyAgent", "EleanorAllyAgent", "LettieAllyAgent", "QuincyAllyAgent")
    return ["/Lotus/Types/Gameplay/1999Wf/ProtoframeAllies/" + name for name in names]


def bounty_cycle(world, manifests):
    hex_entry = next(entry for entry in world["SyndicateMissions"] if entry["Tag"] == "HexSyndicate")
    seed = hex_entry["Seed"]
    faction = ["FC_GRINEER", "FC_CORPUS"][seed & 1]
    allies_by_tag = {"ZarimanSyndicate": None, "EntratiLabSyndicate": None, "HexSyndicate": hex_allies()}
    return {
        "expiry": int(hex_entry["Expiry"]["$date"]["$numberLong"]),
        "zarimanFaction": faction,
        "bounties": {tag: compute_bounties(manifests[tag], seed, faction, allies)
                     for tag, allies in allies_by_tag.items()},
    }


def oracle_disagrees(cycle):
    try:
        oracle = json.loads(fetch_text(ORACLE_URL))
        return oracle["expiry"] == cycle["expiry"] and oracle["bounties"] != cycle["bounties"]
    except Exception:
        return False


def post_worldstate(site_url, key, payload):
    request = urllib.request.Request(
        site_url + "/api/worldstate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "teehistorians-relay"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main():
    site_url = os.environ.get("SITE_URL")
    key = os.environ.get("RELAY_KEY")
    if not site_url or not key:
        sys.exit("Set SITE_URL and RELAY_KEY")
    try:
        world = fetch_worldstate()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        print(f"DE answered {error.code} {body[:200]!r}")
        sys.exit(1)
    except (OSError, ValueError) as error:
        print(str(error)[:300])
        sys.exit(1)
    payload = trim(world)
    try:
        cycle = bounty_cycle(world, fetch_manifests())
        if oracle_disagrees(cycle):
            print("Bounties differ from browse.wf, posting without them")
        else:
            payload["bounties"] = cycle
    except Exception as error:
        print(f"Bounties skipped, {error!r}")
    post_worldstate(site_url, key, payload)
    print("Posted the worldstate")


if __name__ == "__main__":
    main()
