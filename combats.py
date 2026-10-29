#!/usr/bin/env python3
"""Lance les combats solo du jour en parallèle sur Leek Wars.

Authentification (au choix) :
  - LEEKWARS_TOKEN dans le fichier .env à côté du script (ou variable d environnement)
  - sinon le script demande identifiant / mot de passe (non stockés)

Exemples :
  ./combats.py                 # tous les combats restants, 4 en parallèle
  ./combats.py -n 10 -j 8      # 10 combats, 8 en parallèle
  ./combats.py --dry-run       # affiche ce qui serait fait, sans combattre
  ./combats.py --test-notif    # vérifie la notification Telegram

Notification Telegram quand le poireau atteint un niveau de LEVEL_TIPS
(ou de NOTIFY_LEVELS=29,35,38 dans .env) : mettre TELEGRAM_BOT_TOKEN et
TELEGRAM_CHAT_ID dans .env. Le dernier niveau vu est gardé dans .state.json.

Arrêt à un niveau : STOP_LEVEL=29 dans .env (ou --stop-level 29). Le niveau est
relu avant chaque combat ; les combats déjà en cours se terminent quand même.
Une fois l'arrêt atteint, le bot Telegram demande jusqu'à quel niveau continuer :
répondre un niveau (35), « non » (rester à l'arrêt) ou « illimité ». La réponse
est lue au passage suivant du cron. À l'arrêt, le script fait quand même un
combat par jour pour garder la série (streak).

Cron (toutes les 10 min pour lire vite les réponses Telegram ; git pull récupère
la dernière version du dépôt ; flock évite deux lancements en même temps) :
  */10 * * * * cd ~/dev/leekwars && flock -n .lock sh -c 'git pull --ff-only -q; python3 combats.py' >> combats.log 2>&1
"""

import argparse
import getpass
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

API = "https://leekwars.com/api/"
POLL_INTERVAL = 2
FIGHT_TIMEOUT = 180


class LeekWars:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, params=None, retries=6, json_body=None):
        url = API + path
        data = None
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif params is not None:
            data = urllib.parse.urlencode(params).encode()
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                # 429 = trop de requêtes : on attend puis on réessaie
                if e.code == 429 and attempt < retries - 1:
                    try:
                        wait = json.loads(body).get("retry_after", 1)
                    except ValueError:
                        wait = 1
                    time.sleep(wait + attempt + random.random())
                    continue
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {body[:200]}")
            except urllib.error.URLError:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, **params):
        return self.request("POST", path, params)


ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env():
    # Lecteur minimal de .env : lignes CLE=valeur, # pour les commentaires
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def login():
    load_env()
    token = os.environ.get("LEEKWARS_TOKEN")
    if token:
        return token
    if not sys.stdin.isatty():
        sys.exit(f"Pas de jeton : ajoute LEEKWARS_TOKEN=... dans {ENV_FILE}")
    user = input("Identifiant Leek Wars : ")
    password = getpass.getpass("Mot de passe : ")
    res = LeekWars(None).post("farmer/login-token", login=user, password=password)
    if "token" not in res:
        sys.exit(f"Connexion refusée : {res}")
    # On garde le jeton (jamais le mot de passe) pour les prochaines fois
    with open(os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(f"LEEKWARS_TOKEN={res['token']}\n")
    return res["token"]


def pick_opponent(opponents, strategy):
    if strategy == "random":
        return random.choice(opponents)
    # "weak" : l'adversaire au talent le plus bas
    return min(opponents, key=lambda o: o.get("talent", 0))


def wait_result(lw, fight_id):
    deadline = time.time() + FIGHT_TIMEOUT
    while time.time() < deadline:
        f = lw.get(f"fight/get/{fight_id}")
        if f.get("status") == 2:
            return f
        time.sleep(POLL_INTERVAL)
    return None


STOPPED = "niveau d'arrêt atteint"


def run_fight(lw, leek_id, strategy, lock, stop_level=None):
    # Les demandes d'adversaires et de lancement sont sérialisées pour éviter
    # que deux threads se disputent le même adversaire ; seule l'attente du
    # résultat (le plus long) se fait en parallèle.
    with lock:
        # Niveau relu avant chaque combat : le poireau a pu monter pendant les précédents
        if stop_level and lw.get(f"leek/get/{leek_id}")["level"] >= stop_level:
            return None, STOPPED
        opponents = lw.get(f"garden/get-leek-opponents/{leek_id}").get("opponents", [])
        if not opponents:
            return None, "aucun adversaire disponible"
        target = pick_opponent(opponents, strategy)
        res = lw.post("garden/start-solo-fight", leek_id=leek_id, target_id=target["id"])
    fight_id = res.get("fight")
    if not fight_id:
        return None, f"lancement refusé : {res}"

    f = wait_result(lw, fight_id)
    if f is None:
        return fight_id, f"{target['name']:<20} pas de résultat après {FIGHT_TIMEOUT}s"

    my_team = 1 if any(l["id"] == leek_id for l in f["leeks1"]) else 2
    if f["winner"] == 0:
        outcome = "egalite"
    elif f["winner"] == my_team:
        outcome = "victoire"
    else:
        outcome = "defaite"
    return fight_id, (outcome, target)


# Niveaux qui débloquent un achat utile (surcharge possible avec NOTIFY_LEVELS=29,35 dans .env)
LEVEL_TIPS = {
    29: "achète flame et mets-la à la place de rock",
    35: "achète shield et mets-la à la place de helmet",
    38: "achète le laser et mets-le à la place du pistolet",
    42: "achète venom et mets-la à la place de la puce de dégâts la plus faible",
    50: "achète stalactite ou shuriken",
}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state.json")


def notify_levels():
    raw = os.environ.get("NOTIFY_LEVELS")
    if raw:
        return sorted(int(x) for x in raw.split(",") if x.strip())
    return sorted(LEVEL_TIPS)


def telegram(text, method="sendMessage", **params):
    """Appelle l'API du bot ; par défaut envoie text dans le chat configuré.
    Renvoie la réponse décodée, ou None en cas d'échec."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram non configuré : TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents de .env")
        return None
    if method == "sendMessage":
        params.update(chat_id=chat_id, text=text)
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Telegram explique l'erreur dans "description" (chat introuvable, jeton invalide...)
        try:
            reason = json.loads(e.read().decode()).get("description", e)
        except ValueError:
            reason = e
        print(f"Telegram en échec : {reason}")
        return None
    except urllib.error.URLError as e:
        print(f"Telegram en échec : {e}")
        return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def check_level(state, leek):
    """Envoie une notification pour chaque niveau surveillé atteint depuis le dernier passage."""
    key = str(leek["id"])
    level = leek["level"]
    last = state.get(key)
    if last is not None:
        for target in notify_levels():
            if last < target <= level:
                tip = LEVEL_TIPS.get(target, "regarde ce qui se débloque au marché")
                if not telegram(f"🥬 {leek['name']} est niveau {level} ! Niveau {target} atteint : {tip}."):
                    return  # on réessaiera au prochain passage
    state[key] = level


def read_replies(state, level):
    """Lit les messages envoyés au bot depuis le dernier passage. Un nombre
    supérieur au niveau actuel devient le nouveau niveau d'arrêt ; "non" laisse
    le poireau à l'arrêt, "illimité" retire la limite."""
    res = telegram(None, "getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    if not res:
        return
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    for update in res.get("result", []):
        state["tg_offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        # Seul le chat configuré peut piloter le poireau
        if str(msg.get("chat", {}).get("id")) != chat_id:
            continue
        text = msg.get("text", "").strip().lower().lstrip("/")
        number = "".join(c for c in text if c.isdigit())
        if number and int(number) > level:
            state["stop_level"] = int(number)
            state.pop("asked", None)
            telegram(f"👍 C'est noté : combats jusqu'au niveau {number}.")
        elif number:
            telegram(f"Le poireau est déjà niveau {level} : donne un niveau plus haut.")
        elif text in ("illimité", "illimite", "sans limite", "off"):
            state["stop_level"] = None
            state.pop("asked", None)
            telegram("👍 C'est noté : plus de niveau d'arrêt.")
        elif text in ("non", "stop", "pause"):
            telegram(f"⏸️ Le poireau reste au niveau {level}. Envoie un niveau pour repartir.")
        else:
            telegram("Envoie un niveau (ex. 35), « non » pour rester à l'arrêt ou « illimité ».")


def ask_next_level(state, leek):
    """Pose la question une seule fois par niveau atteint."""
    if state.get("asked") == leek["level"]:
        return
    if telegram(f"🛑 {leek['name']} a atteint le niveau {leek['level']} : je n'ai plus lancé de combats. "
                "Jusqu'à quel niveau je continue ? (ex. 35, « non » ou « illimité »)"):
        state["asked"] = leek["level"]


def main():
    parser = argparse.ArgumentParser(description="Combats solo Leek Wars en parallèle")
    parser.add_argument("-n", "--count", type=int, help="nombre de combats (défaut : tous)")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="combats en parallèle (défaut : 4)")
    parser.add_argument("-l", "--leek", type=int, help="id du poireau (défaut : le premier)")
    parser.add_argument("-s", "--strategy", choices=["weak", "random"], default="weak",
                        help="choix de l'adversaire (défaut : talent le plus bas)")
    parser.add_argument("--dry-run", action="store_true", help="ne lance aucun combat")
    parser.add_argument("--test-notif", action="store_true", help="envoie une notification Telegram de test")
    parser.add_argument("--stop-level", type=int,
                        help="ne plus combattre une fois ce niveau atteint (défaut : STOP_LEVEL de .env)")
    args = parser.parse_args()

    lw = LeekWars(login())
    if args.test_notif:
        sys.exit(0 if telegram("🥬 Test : les notifications Leek Wars fonctionnent.") else 1)
    print(time.strftime("[%Y-%m-%d %H:%M]"), end=" ")

    farmer = lw.get("farmer/get-from-token")["farmer"]
    leeks = list(farmer["leeks"].values())
    leek = next((l for l in leeks if l["id"] == args.leek), None) if args.leek else leeks[0]
    if leek is None:
        sys.exit(f"Poireau {args.leek} introuvable. Poireaux : {[(l['id'], l['name']) for l in leeks]}")

    # Niveau d'arrêt : --stop-level, sinon celui choisi sur Telegram, sinon STOP_LEVEL de .env
    state = load_state()
    if args.stop_level:
        state["stop_level"] = args.stop_level
    elif "stop_level" not in state:
        state["stop_level"] = int(os.environ.get("STOP_LEVEL") or 0) or None
    if os.environ.get("TELEGRAM_BOT_TOKEN") and not args.dry_run:
        read_replies(state, leek["level"])
    stop_level = state["stop_level"]

    available = lw.get("garden/get")["garden"]["fights"]
    count = min(args.count or available, available)
    fight_stop_level = stop_level
    if stop_level and leek["level"] >= stop_level:
        # À l'arrêt : un seul combat par jour pour garder la série (streak)
        count = 1 if available and state.get("fight_day") != leekwars_day() else 0
        fight_stop_level = None
    print(f"Fermier {farmer['name']} | poireau {leek['name']} (niv. {leek['level']}"
          + (f", arrêt au niv. {stop_level}" if stop_level else "") + ") | "
          f"{available} combats dispo -> {count} à lancer, {args.jobs} en parallèle")
    if args.dry_run:
        return
    if count == 0:
        finish(state, leek, stop_level)
        return

    stats = {"victoire": 0, "defaite": 0, "egalite": 0, "erreur": 0}
    lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_fight, lw, leek["id"], args.strategy, lock, fight_stop_level)
                   for _ in range(count)]
        try:
            for fut in as_completed(futures):
                done += 1
                try:
                    fight_id, result = fut.result()
                except Exception as e:
                    stats["erreur"] += 1
                    print(f"[{done:>3}/{count}] erreur : {e}")
                    continue
                if isinstance(result, tuple):
                    outcome, target = result
                    stats[outcome] += 1
                    print(f"[{done:>3}/{count}] {outcome:<8} vs {target['name']:<20} "
                          f"(niv. {target['level']}) https://leekwars.com/fight/{fight_id}")
                elif result == STOPPED:
                    if not stats.get("arret"):
                        print(f"Niveau {stop_level} atteint : plus de nouveaux combats")
                    stats["arret"] = stats.get("arret", 0) + 1
                else:
                    stats["erreur"] += 1
                    print(f"[{done:>3}/{count}] {result}")
        except KeyboardInterrupt:
            print("\nInterruption : annulation des combats pas encore lancés...")
            for fut in futures:
                fut.cancel()

    played = stats["victoire"] + stats["defaite"] + stats["egalite"]
    ratio = f"{100 * stats['victoire'] / played:.0f}%" if played else "-"
    print(f"\nBilan : {stats['victoire']} V / {stats['defaite']} D / {stats['egalite']} N "
          f"({ratio} de victoires), {stats['erreur']} erreurs")

    if played:
        state["fight_day"] = leekwars_day()
    leeks = lw.get("farmer/get-from-token")["farmer"]["leeks"]
    finish(state, leeks[str(leek["id"])], stop_level)


def leekwars_day():
    # Les combats du jour se rechargent à minuit, heure de Paris
    return datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()


def finish(state, leek, stop_level):
    check_level(state, leek)
    if stop_level and leek["level"] >= stop_level:
        ask_next_level(state, leek)
    save_state(state)


if __name__ == "__main__":
    main()
