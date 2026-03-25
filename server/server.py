import asyncio
import json
import math
import random
import string
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Location data (loaded at startup) ---
LOCATIONS_CITY: list[dict] = []
LOCATIONS_CENTER: list[dict] = []


@app.on_event("startup")
async def load_locations():
    global LOCATIONS_CITY, LOCATIONS_CENTER
    import httpx
    base = "https://slim-shaldy.github.io/minskguessr"
    async with httpx.AsyncClient() as c:
        r1 = await c.get(f"{base}/locations-yandex.json")
        LOCATIONS_CITY = r1.json()
        r2 = await c.get(f"{base}/locations-yandex-center.json")
        LOCATIONS_CENTER = r2.json()
    print(f"Loaded {len(LOCATIONS_CITY)} city, {len(LOCATIONS_CENTER)} center locations")


# --- Data structures ---
@dataclass
class Player:
    id: str
    nickname: str
    ws: WebSocket
    is_creator: bool = False
    scores: list = field(default_factory=list)
    guesses: list = field(default_factory=list)  # per round: {lat, lng, dist, score, time_sec} or None


@dataclass
class Room:
    code: str
    mode: str  # city | center
    num_rounds: int
    max_players: int
    status: str = "lobby"  # lobby | playing | finished
    current_round: int = 0
    locations: list = field(default_factory=list)
    players: dict = field(default_factory=dict)  # id -> Player
    round_start_time: float = 0
    round_timer_task: asyncio.Task = field(default=None, repr=False)
    created_at: float = field(default_factory=time.time)


rooms: dict[str, Room] = {}


# --- Helpers ---
def gen_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in rooms:
            return code


def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    dLat = math.radians(lat2 - lat1)
    dLng = math.radians(lng2 - lng1)
    a = math.sin(dLat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calc_score(dist_km: float, mode: str) -> int:
    cutoff = 2.0 if mode == "center" else 5.0
    if dist_km >= cutoff:
        return 0
    t = 1 - (dist_km / cutoff)
    return round(5000 * (t ** 1.8))


def get_multiplier(sec: int) -> float:
    if sec <= 25:
        return 1.0
    if sec <= 35:
        return 0.9
    if sec <= 50:
        return 0.8
    if sec <= 70:
        return 0.7
    return 0.0


async def broadcast(room: Room, msg: dict):
    data = json.dumps(msg)
    disconnected = []
    for pid, p in room.players.items():
        try:
            await p.ws.send_text(data)
        except Exception:
            disconnected.append(pid)
    for pid in disconnected:
        room.players.pop(pid, None)


async def send(ws: WebSocket, msg: dict):
    await ws.send_text(json.dumps(msg))


def lobby_update(room: Room) -> dict:
    return {
        "type": "lobby_update",
        "room_code": room.code,
        "mode": room.mode,
        "num_rounds": room.num_rounds,
        "max_players": room.max_players,
        "players": [
            {"nickname": p.nickname, "is_creator": p.is_creator}
            for p in room.players.values()
        ]
    }


def pick_locations(mode: str, count: int) -> list[dict]:
    pool = LOCATIONS_CENTER if mode == "center" else LOCATIONS_CITY
    chosen = random.sample(pool, min(count, len(pool)))
    return [{"lat": loc["lat"], "lng": loc["lng"]} for loc in chosen]


async def start_round(room: Room):
    room.round_start_time = time.time()
    loc = room.locations[room.current_round]

    # Reset round guesses
    for p in room.players.values():
        if len(p.guesses) <= room.current_round:
            p.guesses.append(None)

    await broadcast(room, {
        "type": "round_start",
        "round_num": room.current_round + 1,
        "total_rounds": room.num_rounds,
        "location": loc
    })

    # Auto-timeout after 70 seconds
    room.round_timer_task = asyncio.create_task(round_timeout(room))


async def round_timeout(room: Room):
    await asyncio.sleep(72)  # 70 + 2s buffer
    if room.status != "playing":
        return
    # Fill in 0 for anyone who hasn't guessed
    for p in room.players.values():
        if len(p.guesses) <= room.current_round or p.guesses[room.current_round] is None:
            if len(p.guesses) <= room.current_round:
                p.guesses.append(None)
            p.guesses[room.current_round] = {"lat": 0, "lng": 0, "dist": 999, "score": 0, "time_sec": 70, "timed_out": True}
            p.scores.append(0) if len(p.scores) <= room.current_round else None
    await send_round_result(room)


def all_guessed(room: Room) -> bool:
    for p in room.players.values():
        if len(p.guesses) <= room.current_round or p.guesses[room.current_round] is None:
            return False
    return True


async def send_round_result(room: Room):
    if room.round_timer_task and not room.round_timer_task.done():
        room.round_timer_task.cancel()

    loc = room.locations[room.current_round]
    results = []
    for p in room.players.values():
        g = p.guesses[room.current_round] if len(p.guesses) > room.current_round else None
        if g and not g.get("timed_out"):
            results.append({
                "nickname": p.nickname,
                "score": g["score"],
                "distance": round(g["dist"], 3),
                "time_sec": g["time_sec"],
                "guess_lat": g["lat"],
                "guess_lng": g["lng"],
                "timed_out": False
            })
        else:
            results.append({
                "nickname": p.nickname,
                "score": 0,
                "distance": None,
                "time_sec": 70,
                "guess_lat": None,
                "guess_lng": None,
                "timed_out": True
            })

    results.sort(key=lambda x: x["score"], reverse=True)

    is_last = room.current_round >= room.num_rounds - 1

    await broadcast(room, {
        "type": "round_result",
        "round_num": room.current_round + 1,
        "location": loc,
        "results": results,
        "is_last_round": is_last
    })

    if is_last:
        room.status = "finished"
        leaderboard = []
        for p in room.players.values():
            leaderboard.append({
                "nickname": p.nickname,
                "total_score": sum(p.scores),
                "scores": list(p.scores),
                "is_creator": p.is_creator
            })
        leaderboard.sort(key=lambda x: x["total_score"], reverse=True)
        await broadcast(room, {"type": "game_over", "leaderboard": leaderboard})


# --- WebSocket handler ---
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    player_id = ''.join(random.choices(string.ascii_lowercase, k=12))
    player_room: Room | None = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "create_room":
                code = gen_code()
                room = Room(
                    code=code,
                    mode=msg.get("mode", "city"),
                    num_rounds=min(max(int(msg.get("num_rounds", 5)), 3), 10),
                    max_players=min(max(int(msg.get("max_players", 4)), 2), 8),
                )
                room.locations = pick_locations(room.mode, room.num_rounds)
                player = Player(id=player_id, nickname=msg.get("nickname", "Player"), ws=ws, is_creator=True)
                room.players[player_id] = player
                rooms[code] = room
                player_room = room
                await send(ws, {"type": "room_created", "room_code": code})
                await broadcast(room, lobby_update(room))

            elif t == "join_room":
                code = msg.get("room_code", "").upper().strip()
                room = rooms.get(code)
                if not room:
                    await send(ws, {"type": "error", "message": "Комната не найдена"})
                    continue
                if room.status != "lobby":
                    await send(ws, {"type": "error", "message": "Игра уже идёт"})
                    continue
                if len(room.players) >= room.max_players:
                    await send(ws, {"type": "error", "message": "Комната полная"})
                    continue
                player = Player(id=player_id, nickname=msg.get("nickname", "Player"), ws=ws)
                room.players[player_id] = player
                player_room = room
                await broadcast(room, lobby_update(room))

            elif t == "start_game":
                if not player_room or player_room.status != "lobby":
                    continue
                p = player_room.players.get(player_id)
                if not p or not p.is_creator:
                    await send(ws, {"type": "error", "message": "Только создатель может начать"})
                    continue
                player_room.status = "playing"
                player_room.current_round = 0
                await start_round(player_room)

            elif t == "submit_guess":
                if not player_room or player_room.status != "playing":
                    continue
                p = player_room.players.get(player_id)
                if not p:
                    continue
                # Already guessed this round?
                if len(p.guesses) > player_room.current_round and p.guesses[player_room.current_round] is not None:
                    continue

                elapsed = time.time() - player_room.round_start_time
                time_sec = min(int(elapsed), 70)
                mult = get_multiplier(time_sec)

                loc = player_room.locations[player_room.current_round]
                glat, glng = float(msg["lat"]), float(msg["lng"])
                dist = haversine(glat, glng, loc["lat"], loc["lng"])
                base_score = calc_score(dist, player_room.mode)
                final_score = round(base_score * mult)

                while len(p.guesses) <= player_room.current_round:
                    p.guesses.append(None)
                p.guesses[player_room.current_round] = {
                    "lat": glat, "lng": glng,
                    "dist": dist, "score": final_score,
                    "time_sec": time_sec
                }
                while len(p.scores) <= player_room.current_round:
                    p.scores.append(0)
                p.scores[player_room.current_round] = final_score

                # Notify others
                await broadcast(player_room, {"type": "player_guessed", "nickname": p.nickname})

                if all_guessed(player_room):
                    await send_round_result(player_room)

            elif t == "next_round":
                if not player_room or player_room.status != "playing":
                    continue
                p = player_room.players.get(player_id)
                if not p or not p.is_creator:
                    continue
                player_room.current_round += 1
                if player_room.current_round < player_room.num_rounds:
                    await start_round(player_room)

    except WebSocketDisconnect:
        if player_room and player_id in player_room.players:
            was_creator = player_room.players[player_id].is_creator
            del player_room.players[player_id]
            if not player_room.players:
                rooms.pop(player_room.code, None)
            else:
                if was_creator:
                    # Promote first remaining player
                    next_player = next(iter(player_room.players.values()))
                    next_player.is_creator = True
                await broadcast(player_room, lobby_update(player_room))
    except Exception:
        pass


# --- Cleanup task ---
@app.on_event("startup")
async def cleanup_loop():
    async def _clean():
        while True:
            await asyncio.sleep(300)
            now = time.time()
            expired = [code for code, r in rooms.items() if now - r.created_at > 3600 or not r.players]
            for code in expired:
                rooms.pop(code, None)
    asyncio.create_task(_clean())


@app.get("/health")
async def health():
    return {"status": "ok", "rooms": len(rooms)}
