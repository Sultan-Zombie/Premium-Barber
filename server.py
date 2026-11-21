from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "premium_barber.sqlite3"
SESSION_COOKIE = "premium_barber_session"
SESSION_LIFETIME = 8 * 60 * 60
VALID_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
DEFAULT_SCHEDULE = {
    "start": "09:00",
    "end": "17:00",
    "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
}
STYLES = {
    "classic": {"name": "Classic cut", "price": 2500, "minutes": 45},
    "fade": {"name": "Skin fade", "price": 3500, "minutes": 60},
    "taper": {"name": "Taper fade", "price": 3500, "minutes": 60},
    "scissor": {"name": "Scissor cut", "price": 4000, "minutes": 60},
    "afro": {"name": "Afro shape-up", "price": 3000, "minutes": 45},
    "design": {"name": "Custom design", "price": 5500, "minutes": 75},
}
SERVICES = {
    "beard": {"name": "Beard sculpt & line-up", "price": 2000, "minutes": 20},
    "wash": {"name": "Wash & condition", "price": 1800, "minutes": 15},
    "steaming": {"name": "Hot towel treatment", "price": 2500, "minutes": 20},
    "colour": {"name": "Hair colour refresh", "price": 6500, "minutes": 45},
}
BARBERS = {"Msanii", "Gadaffi", "Eric"}
LOCATIONS = {"Mombasa", "Nairobi"}
PAYMENT_METHODS = {"M-Pesa", "Google Pay", "Bank transfer", "Card", "Pay at shop"}
SESSIONS: dict[str, tuple[str, float]] = {}
LOGIN_FAILURES: dict[str, list[float]] = {}
STATE_LOCK = threading.Lock()


def load_local_environment() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if separator and key and not key.startswith("#"):
            os.environ.setdefault(key, value)


@contextmanager
def connect_database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with connect_database() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS appointments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                style_id TEXT NOT NULL,
                service_ids TEXT NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                payment TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                barber TEXT NOT NULL DEFAULT 'Msanii',
                location TEXT NOT NULL DEFAULT 'Nairobi',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        appointment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(appointments)")}
        if "barber" not in appointment_columns:
            connection.execute("ALTER TABLE appointments ADD COLUMN barber TEXT NOT NULL DEFAULT 'Msanii'")
        if "location" not in appointment_columns:
            connection.execute("ALTER TABLE appointments ADD COLUMN location TEXT NOT NULL DEFAULT 'Nairobi'")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id TEXT NOT NULL UNIQUE REFERENCES appointments(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                review TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("schedule", json.dumps(DEFAULT_SCHEDULE)),
        )


def read_schedule(connection: sqlite3.Connection) -> dict:
    row = connection.execute("SELECT value FROM settings WHERE key = 'schedule'").fetchone()
    return json.loads(row["value"]) if row else dict(DEFAULT_SCHEDULE)


def appointment_from_row(row: sqlite3.Row) -> dict:
    appointment = dict(row)
    appointment["styleId"] = appointment.pop("style_id")
    appointment["serviceIds"] = json.loads(appointment.pop("service_ids"))
    appointment["date"] = appointment.pop("appointment_date")
    appointment["time"] = appointment.pop("appointment_time")
    appointment["createdAt"] = appointment.pop("created_at")
    appointment["updatedAt"] = appointment.pop("updated_at")
    return appointment


def duration_for(style_id: str, service_ids: list[str]) -> int:
    return STYLES[style_id]["minutes"] + sum(SERVICES[service_id]["minutes"] for service_id in service_ids)


def parse_phone(phone: str) -> str:
    return "".join(character for character in phone if character.isdigit())


def available_times(
    connection: sqlite3.Connection,
    appointment_date: str,
    duration: int,
    barber: str,
    except_id: str | None = None,
) -> list[str]:
    try:
        selected_date = date.fromisoformat(appointment_date)
    except (TypeError, ValueError):
        return []
    if selected_date < date.today():
        return []
    schedule = read_schedule(connection)
    if VALID_DAYS[selected_date.weekday() + 1 if selected_date.weekday() < 6 else 0] not in schedule["days"]:
        return []
    start_minutes = int(schedule["start"][:2]) * 60 + int(schedule["start"][3:])
    end_minutes = int(schedule["end"][:2]) * 60 + int(schedule["end"][3:])
    rows = connection.execute(
        "SELECT * FROM appointments WHERE appointment_date = ? AND barber = ? AND status != 'rejected'",
        (appointment_date, barber),
    ).fetchall()
    occupied = []
    for row in rows:
        if row["id"] == except_id:
            continue
        start = datetime.fromisoformat(f"{row['appointment_date']}T{row['appointment_time']}")
        end = start + timedelta(minutes=duration_for(row["style_id"], json.loads(row["service_ids"])))
        occupied.append((start, end))
    times = []
    for minute in range(start_minutes, end_minutes - duration + 1, 30):
        hour, minute_part = divmod(minute, 60)
        time_value = f"{hour:02d}:{minute_part:02d}"
        start = datetime.fromisoformat(f"{appointment_date}T{time_value}")
        end = start + timedelta(minutes=duration)
        if appointment_date == date.today().isoformat() and start <= datetime.now():
            continue
        if any(start < busy_end and busy_start < end for busy_start, busy_end in occupied):
            continue
        times.append(time_value)
    return times


class BarberHandler(BaseHTTPRequestHandler):
    server_version = "PremiumBarber/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {format_string % args}")

    def send_json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64_000:
                raise ValueError("Request is too large.")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object.")
            return value
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid request body.") from error

    def session_role(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        session_id = morsel.value
        with STATE_LOCK:
            session = SESSIONS.get(session_id)
            if not session:
                return None
            role, expires_at = session
            if expires_at <= time.time():
                SESSIONS.pop(session_id, None)
                return None
            return role

    def require_role(self, *roles: str) -> bool:
        role = self.session_role()
        if role not in roles:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "This page is not available for your sign-in."})
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_index()
            return
        if parsed.path == "/api/session":
            self.send_json(HTTPStatus.OK, {"role": self.session_role()})
            return
        if parsed.path == "/api/schedule":
            with connect_database() as connection:
                self.send_json(HTTPStatus.OK, {"schedule": read_schedule(connection)})
            return
        if parsed.path == "/api/availability":
            query = parse_qs(parsed.query)
            style_id = query.get("style", [""])[0]
            service_ids = [value for value in query.get("services", [""])[0].split(",") if value]
            appointment_date = query.get("date", [""])[0]
            barber = query.get("barber", [""])[0]
            if style_id not in STYLES or any(service_id not in SERVICES for service_id in service_ids) or barber not in BARBERS:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a valid cut and services."})
                return
            except_id = query.get("except", [""])[0] or None
            if except_id and not self.require_role("barber"):
                return
            with connect_database() as connection:
                times = available_times(connection, appointment_date, duration_for(style_id, service_ids), barber, except_id)
            self.send_json(HTTPStatus.OK, {"times": times})
            return
        if parsed.path == "/api/appointments":
            if not self.require_role("barber", "admin"):
                return
            with connect_database() as connection:
                rows = connection.execute(
                    "SELECT * FROM appointments ORDER BY appointment_date, appointment_time"
                ).fetchall()
            self.send_json(HTTPStatus.OK, {"appointments": [appointment_from_row(row) for row in rows]})
            return
        if parsed.path == "/api/reviews":
            with connect_database() as connection:
                rows = connection.execute(
                    """SELECT r.rating, r.review, r.created_at, a.name, a.style_id, a.service_ids
                    FROM reviews r JOIN appointments a ON a.id = r.appointment_id
                    ORDER BY r.created_at DESC LIMIT 6"""
                ).fetchall()
                summary = connection.execute("SELECT COUNT(*) AS count, AVG(rating) AS average FROM reviews").fetchone()
            reviews = []
            for row in rows:
                style = STYLES[row["style_id"]]["name"]
                service_ids = json.loads(row["service_ids"])
                service_names = [style, *(SERVICES[service_id]["name"] for service_id in service_ids)]
                reviews.append({
                    "author": row["name"].split()[0],
                    "service": " · ".join(service_names),
                    "rating": row["rating"],
                    "review": row["review"],
                    "createdAt": row["created_at"],
                })
            self.send_json(HTTPStatus.OK, {
                "reviews": reviews,
                "count": summary["count"],
                "average": round(summary["average"], 1) if summary["average"] is not None else None,
            })
            return
        if parsed.path == "/api/my-visits":
            phone = parse_qs(parsed.query).get("phone", [""])[0]
            if len(parse_phone(phone)) < 7:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Enter a valid phone number."})
                return
            with connect_database() as connection:
                rows = connection.execute(
                    "SELECT * FROM appointments ORDER BY appointment_date DESC, appointment_time DESC"
                ).fetchall()
                matches = []
                for row in rows:
                    if parse_phone(row["phone"]) != parse_phone(phone):
                        continue
                    appointment = appointment_from_row(row)
                    review = connection.execute(
                        "SELECT rating, review, created_at FROM reviews WHERE appointment_id = ?",
                        (appointment["id"],),
                    ).fetchone()
                    appointment["reviewed"] = review is not None
                    if review:
                        appointment["review"] = dict(review)
                    matches.append(appointment)
            self.send_json(HTTPStatus.OK, {"appointments": matches})
            return
        if parsed.path == "/api/export":
            if not self.require_role("database"):
                return
            with connect_database() as connection:
                appointments = [
                    appointment_from_row(row)
                    for row in connection.execute("SELECT * FROM appointments ORDER BY created_at DESC").fetchall()
                ]
                reviews = [dict(row) for row in connection.execute(
                    """SELECT r.id, r.appointment_id, r.rating, r.review, r.created_at
                    FROM reviews r ORDER BY r.created_at DESC"""
                ).fetchall()]
                schedule = read_schedule(connection)
            self.send_json(HTTPStatus.OK, {"appointments": appointments, "reviews": reviews, "schedule": schedule})
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if parsed.path == "/api/login":
            self.login(payload)
        elif parsed.path == "/api/logout":
            self.logout()
        elif parsed.path == "/api/appointments":
            self.create_appointment(payload)
        elif parsed.path == "/api/reviews":
            self.create_review(payload)
        elif parsed.path == "/api/schedule":
            self.update_schedule(payload)
        elif parsed.path.startswith("/api/appointments/") and parsed.path.endswith("/action"):
            self.update_appointment(parsed.path.split("/")[3], payload)
        elif parsed.path == "/api/database/clear":
            if self.require_role("database"):
                with connect_database() as connection:
                    connection.execute("DELETE FROM appointments")
                self.send_json(HTTPStatus.OK, {"ok": True})
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def serve_index(self) -> None:
        content = (ROOT / "index.html").read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def login(self, payload: dict) -> None:
        role = payload.get("role")
        password = payload.get("password")
        if not isinstance(role, str) or role not in {"admin", "barber", "database"} or not isinstance(password, str):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a workspace and enter its password."})
            return
        configured_password = os.environ.get({"admin": "ADMIN_PASSWORD", "barber": "BARBER_PASSWORD", "database": "DATABASE_PASSWORD"}[role], "")
        if not configured_password:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "This workspace password is not configured in the local .env file."})
            return
        client = self.client_address[0]
        now = time.time()
        with STATE_LOCK:
            recent = [attempt for attempt in LOGIN_FAILURES.get(client, []) if now - attempt < 600]
            LOGIN_FAILURES[client] = recent
            if len(recent) >= 8:
                self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many sign-in attempts. Try again in ten minutes."})
                return
        if not hmac.compare_digest(password.encode("utf-8"), configured_password.encode("utf-8")):
            with STATE_LOCK:
                LOGIN_FAILURES.setdefault(client, []).append(now)
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "That password did not match this workspace."})
            return
        with STATE_LOCK:
            LOGIN_FAILURES.pop(client, None)
            session_id = secrets.token_urlsafe(32)
            SESSIONS[session_id] = (role, now + SESSION_LIFETIME)
        cookie = f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_LIFETIME}"
        self.send_json(HTTPStatus.OK, {"role": role}, {"Set-Cookie": cookie})

    def logout(self) -> None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if morsel:
            with STATE_LOCK:
                SESSIONS.pop(morsel.value, None)
        expired_cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        self.send_json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": expired_cookie})

    def create_review(self, payload: dict) -> None:
        appointment_id = payload.get("appointmentId")
        phone = payload.get("phone")
        rating = payload.get("rating")
        review_text = payload.get("review")
        if (
            not isinstance(appointment_id, str)
            or not isinstance(phone, str)
            or not isinstance(rating, int)
            or isinstance(rating, bool)
            or not 1 <= rating <= 5
            or not isinstance(review_text, str)
            or not 10 <= len(review_text.strip()) <= 800
        ):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a 1–5 star rating and write a review of 10–800 characters."})
            return
        with connect_database() as connection:
            appointment = connection.execute(
                "SELECT phone, status FROM appointments WHERE id = ?",
                (appointment_id,),
            ).fetchone()
            if not appointment or parse_phone(appointment["phone"]) != parse_phone(phone):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "No matching appointment was found for that phone number."})
                return
            if appointment["status"] not in {"accepted", "rescheduled"}:
                self.send_json(HTTPStatus.CONFLICT, {"error": "Reviews are available after the barber confirms your appointment."})
                return
            try:
                connection.execute(
                    "INSERT INTO reviews (appointment_id, rating, review, created_at) VALUES (?, ?, ?, ?)",
                    (appointment_id, rating, review_text.strip(), datetime.now().isoformat(timespec="seconds")),
                )
            except sqlite3.IntegrityError:
                self.send_json(HTTPStatus.CONFLICT, {"error": "This appointment has already been reviewed."})
                return
        self.send_json(HTTPStatus.CREATED, {"ok": True})

    def create_appointment(self, payload: dict) -> None:
        name = payload.get("name", "").strip() if isinstance(payload.get("name"), str) else ""
        phone = payload.get("phone", "").strip() if isinstance(payload.get("phone"), str) else ""
        style_id = payload.get("styleId")
        barber = payload.get("barber")
        location = payload.get("location")
        service_ids = payload.get("serviceIds", [])
        appointment_date = payload.get("date", "")
        appointment_time = payload.get("time", "")
        payment = payload.get("payment")
        note = payload.get("note", "")
        if (
            not 1 <= len(name) <= 80
            or not 7 <= len(parse_phone(phone)) <= 20
            or not isinstance(style_id, str)
            or style_id not in STYLES
            or not isinstance(barber, str)
            or barber not in BARBERS
            or not isinstance(location, str)
            or location not in LOCATIONS
            or not isinstance(service_ids, list)
            or any(not isinstance(service_id, str) or service_id not in SERVICES for service_id in service_ids)
            or len(set(service_ids)) != len(service_ids)
            or not isinstance(payment, str)
            or payment not in PAYMENT_METHODS
            or not isinstance(appointment_date, str)
            or not isinstance(appointment_time, str)
            or not isinstance(note, str)
            or len(note) > 180
        ):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Check your details and selected services."})
            return
        total = STYLES[style_id]["price"] + sum(SERVICES[service_id]["price"] for service_id in service_ids)
        if not 1500 <= total <= 80000:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "The appointment total must be between KSh 1,500 and KSh 80,000."})
            return
        try:
            selected_date = date.fromisoformat(appointment_date)
            selected_time = datetime.strptime(appointment_time, "%H:%M").time()
        except (TypeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a valid appointment date and time."})
            return
        if selected_date < date.today():
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a future appointment date."})
            return
        now = datetime.now().isoformat(timespec="seconds")
        appointment_id = f"pb-{uuid4().hex}"
        with connect_database() as connection:
            options = available_times(connection, appointment_date, duration_for(style_id, service_ids), barber)
            if appointment_time not in options:
                self.send_json(HTTPStatus.CONFLICT, {"error": "That time is no longer available. Choose another slot."})
                return
            connection.execute(
                """INSERT INTO appointments
                (id, name, phone, style_id, service_ids, appointment_date, appointment_time,
                 payment, note, barber, location, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (appointment_id, name, phone, style_id, json.dumps(service_ids), appointment_date,
                 selected_time.strftime("%H:%M"), payment, note, barber, location, now, now),
            )
            row = connection.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        self.send_json(HTTPStatus.CREATED, {"appointment": appointment_from_row(row)})

    def update_schedule(self, payload: dict) -> None:
        if not self.require_role("barber"):
            return
        start, end, days = payload.get("start"), payload.get("end"), payload.get("days")
        if (
            not isinstance(start, str)
            or not isinstance(end, str)
            or not isinstance(days, list)
            or not days
            or any(not isinstance(day, str) or day not in VALID_DAYS for day in days)
            or len(set(days)) != len(days)
        ):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose valid working days and hours."})
            return
        try:
            start_time = datetime.strptime(start, "%H:%M").time()
            end_time = datetime.strptime(end, "%H:%M").time()
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Enter valid opening and closing times."})
            return
        if start_time >= end_time:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Closing time must be after opening time."})
            return
        schedule = {"start": start, "end": end, "days": days}
        with connect_database() as connection:
            connection.execute("UPDATE settings SET value = ? WHERE key = 'schedule'", (json.dumps(schedule),))
        self.send_json(HTTPStatus.OK, {"schedule": schedule})

    def update_appointment(self, appointment_id: str, payload: dict) -> None:
        if not self.require_role("barber"):
            return
        action = payload.get("action")
        with connect_database() as connection:
            row = connection.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
            if not row:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Appointment not found."})
                return
            if row["status"] != "pending":
                self.send_json(HTTPStatus.CONFLICT, {"error": "This request has already been handled."})
                return
            now = datetime.now().isoformat(timespec="seconds")
            if action in {"accept", "reject"}:
                status = "accepted" if action == "accept" else "rejected"
                connection.execute("UPDATE appointments SET status = ?, updated_at = ? WHERE id = ?", (status, now, appointment_id))
            elif action == "reschedule":
                appointment_date = payload.get("date", "")
                appointment_time = payload.get("time", "")
                try:
                    datetime.strptime(appointment_time, "%H:%M")
                except (TypeError, ValueError):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a valid new time."})
                    return
                service_ids = json.loads(row["service_ids"])
                times = available_times(connection, appointment_date, duration_for(row["style_id"], service_ids), row["barber"], appointment_id)
                if appointment_time not in times:
                    self.send_json(HTTPStatus.CONFLICT, {"error": "That time is no longer available."})
                    return
                connection.execute(
                    """UPDATE appointments SET appointment_date = ?, appointment_time = ?,
                    status = 'rescheduled', updated_at = ? WHERE id = ?""",
                    (appointment_date, appointment_time, now, appointment_id),
                )
            else:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose accept, reject, or reschedule."})
                return
            updated = connection.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        self.send_json(HTTPStatus.OK, {"appointment": appointment_from_row(updated)})


def main() -> None:
    load_local_environment()
    initialize_database()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), BarberHandler)
    print(f"Premium Barber is available at http://127.0.0.1:{port}")
    if not os.environ.get("BARBER_PASSWORD"):
        print("Set BARBER_PASSWORD in .env before signing in to the barber workspace.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPremium Barber server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()