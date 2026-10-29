#!/usr/bin/env python3
"""Lance des combats de test (scénario de l'éditeur) et résume les résultats.

Les combats de test sont gratuits : ils ne consomment pas les combats du jour.
Authentification : la même que combats.py (LEEKWARS_TOKEN dans .env).

Exemples :
  ./tests.py                   # 20 tests sur le scénario par défaut
  ./tests.py -n 40 -v          # 40 tests, détail de chaque combat
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from combats import LeekWars, login, wait_result

SCENARIO = 94434
AI = "Sans Titre"
LEEK_NAME = "méné"
# Délai entre deux lancements : en dessous l'API répond "rate_limit"
START_DELAY = 4


def summarize(f):
    me = 1 if any(l["name"] == LEEK_NAME for l in f["leeks1"]) else 2
    if f["winner"] == 0:
        outcome = "N"
    elif f["winner"] == me:
        outcome = "V"
    else:
        outcome = "D"
    turns = 1 + sum(1 for a in f["data"]["actions"] if a[0] == 6)
    opponents = ",".join(l["name"] for l in f["leeks2" if me == 1 else "leeks1"])
    return outcome, turns, opponents


def main():
    parser = argparse.ArgumentParser(description="Combats de test Leek Wars")
    parser.add_argument("-n", "--count", type=int, default=20, help="nombre de tests (défaut : 20)")
    parser.add_argument("-s", "--scenario", type=int, default=SCENARIO, help="id du scénario")
    parser.add_argument("-a", "--ai", default=AI, help="nom de l'IA")
    parser.add_argument("-v", "--verbose", action="store_true", help="affiche chaque combat")
    args = parser.parse_args()

    lw = LeekWars(login())
    fight_ids = []
    for i in range(args.count):
        res = lw.request("POST", "ai/test-scenario",
                         json_body={"scenario_id": args.scenario, "ai_id": args.ai})
        if "fight" in res:
            fight_ids.append(res["fight"])
        else:
            print(f"lancement {i + 1} refusé : {res}")
        if i < args.count - 1:
            time.sleep(START_DELAY)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fights = list(pool.map(lambda fid: wait_result(lw, fid), fight_ids))

    stats = {"V": 0, "D": 0, "N": 0}
    turns = []
    for fid, f in zip(fight_ids, fights):
        if f is None:
            print(f"{fid} : pas de résultat")
            continue
        outcome, t, opponents = summarize(f)
        stats[outcome] += 1
        turns.append(t)
        if args.verbose:
            print(f"{outcome} {t:>2} tours vs {opponents:<15} https://leekwars.com/fight/{fid}")

    played = sum(stats.values())
    if played:
        print(f"{stats['V']} V / {stats['D']} D / {stats['N']} N sur {played} "
              f"({100 * stats['V'] / played:.0f}% de victoires), "
              f"{sum(turns) / len(turns):.0f} tours en moyenne")


if __name__ == "__main__":
    main()
