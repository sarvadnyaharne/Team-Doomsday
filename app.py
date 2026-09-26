from flask import Flask, request, jsonify, session, render_template, redirect
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "pulsecheck-demo-secret"
)

# Safe cookie defaults for local HTTP and Vercel HTTPS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL"))
)

# Keep the existing SQLite database for local development.
# Vercel's deployment filesystem is not persistent, so use /tmp there
# rather than attempting to write beside the deployed source files.
if os.environ.get("VERCEL"):
    DB = "/tmp/pulsecheck.db"
else:
    DB = os.path.join(
        os.path.dirname(__file__),
        "pulsecheck.db"
    )


# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(DB, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db():

    c = db()

    c.executescript("""

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'team'
        );

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_members (
            team_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY(team_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS pulses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            accomplished TEXT,
            blocker TEXT,
            next_step TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            risk TEXT NOT NULL,
            created_at TEXT NOT NULL,
            seen INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mentor_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            mentor_id INTEGER NOT NULL,
            solution TEXT NOT NULL,
            created_at TEXT NOT NULL,
            seen INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mentor_tips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            mentor_id INTEGER NOT NULL,
            tip TEXT NOT NULL,
            created_at TEXT NOT NULL,
            seen INTEGER DEFAULT 0
        );

    """)

    # Safe migration for databases created by earlier versions.
    # The old team_members table had no joined_at column, while the
    # current team-selection logic uses it when available.
    try:
        c.execute("ALTER TABLE team_members ADD COLUMN joined_at TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute("UPDATE team_members SET joined_at=COALESCE(joined_at, ?) WHERE joined_at IS NULL",
              (datetime.utcnow().isoformat(),))

    # -----------------------------------------------------
    # DEMO MENTOR
    # -----------------------------------------------------

    mentor = c.execute(
        "SELECT id FROM users WHERE email=?",
        ("mentor@pulsecheck.demo",)
    ).fetchone()

    if not mentor:

        c.execute(
            """
            INSERT INTO users
            (name,email,password,role)
            VALUES(?,?,?,?)
            """,
            (
                "Alex Mentor",
                "mentor@pulsecheck.demo",
                generate_password_hash("mentor123"),
                "mentor"
            )
        )

    # -----------------------------------------------------
    # DEMO TEAM
    # -----------------------------------------------------

    team_user = c.execute(
        "SELECT id FROM users WHERE email=?",
        ("team@pulsecheck.demo",)
    ).fetchone()

    if not team_user:

        c.execute(
            """
            INSERT INTO users
            (name,email,password,role)
            VALUES(?,?,?,?)
            """,
            (
                "Sarvadnya Harne",
                "team@pulsecheck.demo",
                generate_password_hash("team123"),
                "team"
            )
        )

        uid = c.execute(
            """
            SELECT id
            FROM users
            WHERE email=?
            """,
            ("team@pulsecheck.demo",)
        ).fetchone()["id"]

        now = datetime.utcnow().isoformat()

        c.execute(
            """
            INSERT INTO teams
            (name,owner_id,created_at)
            VALUES(?,?,?)
            """,
            (
                "Team Sarva",
                uid,
                now
            )
        )

        tid = c.execute(
            "SELECT last_insert_rowid() id"
        ).fetchone()["id"]

        c.execute(
            """
            INSERT INTO team_members
            (team_id,user_id)
            VALUES(?,?)
            """,
            (tid, uid)
        )

        # Demo pulse
        c.execute(
            """
            INSERT INTO pulses
            (
                team_id,
                user_id,
                status,
                accomplished,
                blocker,
                next_step,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tid,
                uid,
                "blocked",
                "Frontend is completed",
                "Backend is stuck",
                "Find a backend expert",
                datetime.utcnow().isoformat()
            )
        )

    c.commit()
    c.close()


# =========================================================
# USER HELPERS
# =========================================================

def me():

    if not session.get("uid"):
        return None

    c = db()

    user = c.execute(
        """
        SELECT id,name,email,role
        FROM users
        WHERE id=?
        """,
        (session["uid"],)
    ).fetchone()

    c.close()

    return dict(user) if user else None


def my_team(user_id):
    c = db()
    # team_members in older databases does not have an id column.
    # Prefer the latest explicit joined_at value, then fall back to team id.
    try:
        team = c.execute(
            """
            SELECT t.*
            FROM teams t
            JOIN team_members tm ON tm.team_id=t.id
            WHERE tm.user_id=?
            ORDER BY COALESCE(tm.joined_at, t.created_at) DESC, t.id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        team = c.execute(
            """
            SELECT t.*
            FROM teams t
            JOIN team_members tm ON tm.team_id=t.id
            WHERE tm.user_id=?
            ORDER BY t.id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
    c.close()
    return dict(team) if team else None

# =========================================================
# RISK ENGINE
# =========================================================

def risk_for(team_id):

    c = db()

    pulse = c.execute(
        """
        SELECT *
        FROM pulses
        WHERE team_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (team_id,)
    ).fetchone()

    # Optional mentor tip: use the latest tip as additional AI context.
    # This does not change the existing tip feature or database behavior.
    try:
        latest_tip = c.execute(
            """
            SELECT tip
            FROM mentor_tips
            WHERE team_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (team_id,)
        ).fetchone()
        mentor_tip = latest_tip["tip"] if latest_tip else ""
    except sqlite3.OperationalError:
        mentor_tip = ""

    c.close()

    if not pulse:

        return {
            "score": 100,
            "risk": "critical",
            "reasons": [
                "No pulse submitted yet"
            ],
            "hours": 999,
            "last": None
        }

    try:

        created = datetime.fromisoformat(
            pulse["created_at"]
        )

        hours = round(
            (
                datetime.utcnow() - created
            ).total_seconds() / 3600,
            1
        )

    except Exception:

        hours = 0

    score = 0
    reasons = []

    if hours >= 48:

        score += 60

        reasons.append(
            "No pulse for 48+ hours"
        )

    elif hours >= 21:

        score += 35

        reasons.append(
            "Pulse overdue"
        )

    if pulse["status"] == "blocked":

        score += 40

        reasons.append(
            "Team reports a blocker"
        )

    elif pulse["status"] == "at_risk":

        score += 20

        reasons.append(
            "Team reports being stuck"
        )

    score = min(score, 100)

    if score >= 70:
        risk = "critical"

    elif score >= 25:
        risk = "warning"

    else:
        risk = "healthy"

    return {
        "score": score,
        "risk": risk,
        "reasons": reasons or [
            "Recent pulse is healthy"
        ],
        "hours": hours,
        "last": pulse["created_at"]
    }


# =========================================================
# PAGES
# =========================================================

@app.route("/")
def home():

    return render_template(
        "index.html",
        user=me()
    )


@app.route("/login")
def login_page():

    return render_template(
        "login.html"
    )


@app.route("/dashboard")
def dashboard():

    user = me()

    if not user:
        return redirect("/login")

    return render_template(
        "dashboard.html",
        user=user
    )


# =========================================================
# AUTH
# =========================================================

@app.post("/api/auth/login")
def login():

    data = request.get_json(force=True)

    email = data.get(
        "email",
        ""
    ).strip().lower()

    password = data.get(
        "password",
        ""
    )

    c = db()

    user = c.execute(
        """
        SELECT *
        FROM users
        WHERE email=?
        """,
        (email,)
    ).fetchone()

    c.close()

    if (
        not user
        or not check_password_hash(
            user["password"],
            password
        )
    ):

        return jsonify(
            error="Invalid email or password."
        ), 401

    session["uid"] = user["id"]

    return jsonify(
        ok=True,
        user=me()
    )


@app.post("/api/auth/register")
def register():

    data = request.get_json(force=True)

    name = data.get(
        "name",
        ""
    ).strip()

    email = data.get(
        "email",
        ""
    ).strip().lower()

    password = data.get(
        "password",
        ""
    )

    role = data.get(
        "role",
        "team"
    )

    if (
        not name
        or not email
        or len(password) < 6
    ):

        return jsonify(
            error=(
                "Name, email and password "
                "(6+) are required."
            )
        ), 400

    if role not in (
        "team",
        "mentor"
    ):
        role = "team"

    c = db()

    try:

        result = c.execute(
            """
            INSERT INTO users
            (name,email,password,role)
            VALUES(?,?,?,?)
            """,
            (
                name,
                email,
                generate_password_hash(
                    password
                ),
                role
            )
        )

        uid = result.lastrowid

        # Team users choose Create Team or Join Team after signing in.

        c.commit()

    except sqlite3.IntegrityError:

        c.close()

        return jsonify(
            error="Email already exists."
        ), 409

    c.close()

    session["uid"] = uid

    return jsonify(
        ok=True,
        user=me()
    )


@app.post("/api/auth/logout")
def logout():

    session.clear()

    return jsonify(
        ok=True
    )


@app.get("/api/me")
def api_me():

    return jsonify(
        user=me()
    )


# =========================================================
# TEAM MANAGEMENT
# =========================================================

@app.get("/api/teams")
def teams():

    user = me()

    if not user:

        return jsonify(
            error="Login required."
        ), 401

    c = db()

    rows = c.execute(
        """
        SELECT id,name,created_at
        FROM teams
        ORDER BY id DESC
        """
    ).fetchall()

    c.close()

    return jsonify(
        teams=[
            dict(row)
            for row in rows
        ]
    )


@app.post("/api/teams")
def create_team():

    user = me()

    if (
        not user
        or user["role"] != "team"
    ):

        return jsonify(
            error="Team access required."
        ), 403

    data = request.get_json(
        force=True
    )

    name = data.get(
        "name",
        ""
    ).strip()

    if not name:

        return jsonify(
            error="Team name required."
        ), 400

    c = db()

    result = c.execute(
        """
        INSERT INTO teams
        (name,owner_id,created_at)
        VALUES(?,?,?)
        """,
        (
            name,
            user["id"],
            datetime.utcnow().isoformat()
        )
    )

    tid = result.lastrowid

    c.execute(
        """
        INSERT INTO team_members
        (team_id,user_id,joined_at)
        VALUES(?,?,?)
        """,
        (
            tid,
            user["id"],
            datetime.utcnow().isoformat()
        )
    )

    c.commit()
    c.close()

    return jsonify(
        ok=True,
        team={
            "id": tid,
            "name": name
        }
    )


@app.post("/api/teams/<int:tid>/join")
def join_team(tid):

    user = me()

    if (
        not user
        or user["role"] != "team"
    ):

        return jsonify(
            error="Team access required."
        ), 403

    c = db()

    exists = c.execute(
        """
        SELECT id
        FROM teams
        WHERE id=?
        """,
        (tid,)
    ).fetchone()

    if not exists:

        c.close()

        return jsonify(
            error="Team not found."
        ), 404

    try:

        c.execute(
            """
            INSERT INTO team_members
            (team_id,user_id,joined_at)
            VALUES(?,?,?)
            """,
            (
                tid,
                user["id"],
                datetime.utcnow().isoformat()
            )
        )

        c.commit()

    except sqlite3.IntegrityError:

        pass

    c.close()

    return jsonify(
        ok=True
    )


# =========================================================
# PULSES
# =========================================================

@app.route(
    "/api/pulses",
    methods=["GET", "POST"]
)
def pulses():

    user = me()

    if not user:

        return jsonify(
            error="Login required."
        ), 401

    team = my_team(
        user["id"]
    )

    if not team:

        return jsonify(
            error="Join a team first."
        ), 400

    c = db()

    if request.method == "POST":

        data = request.get_json(
            force=True
        )

        status = data.get(
            "status",
            "on_track"
        )

        if status not in (
            "on_track",
            "at_risk",
            "blocked"
        ):

            status = "on_track"

        now = datetime.utcnow().isoformat()

        c.execute(
            """
            INSERT INTO pulses
            (
                team_id,
                user_id,
                status,
                accomplished,
                blocker,
                next_step,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                team["id"],
                user["id"],
                status,
                data.get(
                    "accomplished",
                    ""
                ),
                data.get(
                    "blocker",
                    ""
                ),
                data.get(
                    "next_step",
                    ""
                ),
                now
            )
        )

        c.commit()

        risk = risk_for(
            team["id"]
        )

        if risk["risk"] != "healthy":

            c.execute(
                """
                INSERT INTO alerts
                (
                    team_id,
                    message,
                    risk,
                    created_at
                )
                VALUES(?,?,?,?)
                """,
                (
                    team["id"],
                    " · ".join(
                        risk["reasons"]
                    ),
                    (
                        "critical"
                        if risk["risk"]
                        == "critical"
                        else "warning"
                    ),
                    now
                )
            )

            c.commit()

    rows = c.execute(
        """
        SELECT
            p.*,
            u.name
        FROM pulses p
        JOIN users u
            ON u.id=p.user_id
        WHERE p.team_id=?
        ORDER BY p.created_at DESC
        LIMIT 30
        """,
        (team["id"],)
    ).fetchall()

    c.close()

    return jsonify(
        team=team,
        pulses=[
            dict(row)
            for row in rows
        ]
    )


# =========================================================
# TEAM SUMMARY
# =========================================================

@app.get("/api/team/summary")
def summary():

    user = me()

    if not user:

        return jsonify(
            error="Login required."
        ), 401

    team = my_team(
        user["id"]
    )

    if not team:

        return jsonify(
            error="No team found."
        ), 404

    c = db()

    rows = c.execute(
        """
        SELECT created_at
        FROM pulses
        WHERE team_id=?
        """,
        (team["id"],)
    ).fetchall()

    c.close()

    dates = set()

    for row in rows:

        try:

            dates.add(
                datetime.fromisoformat(
                    row["created_at"]
                ).date()
            )

        except Exception:

            pass

    current = datetime.utcnow().date()

    streak = 0

    while current in dates:

        streak += 1

        current -= timedelta(
            days=1
        )

    risk = risk_for(
        team["id"]
    )

    return jsonify(

        team=team,

        health={
            "healthy": "Healthy",
            "warning": "At Risk",
            "critical": "Critical"
        }[risk["risk"]],

        risk_score=risk["score"],

        risk_reasons=risk["reasons"],

        streak=streak,

        pulse_count=len(rows),

        last_pulse=risk["last"],

        hours_since_pulse=risk["hours"]
    )


# =========================================================
# MENTOR DASHBOARD
# =========================================================

@app.get("/api/mentor/dashboard")
def mentor_dashboard():

    user = me()

    if (
        not user
        or user["role"] != "mentor"
    ):

        return jsonify(
            error="Mentor access required."
        ), 403

    c = db()

    rows = c.execute(
        """
        SELECT id,name
        FROM teams
        ORDER BY id DESC
        """
    ).fetchall()

    result = []

    for team in rows:

        risk = risk_for(
            team["id"]
        )

        result.append({

            "id": team["id"],

            "name": team["name"],

            **risk

        })

    c.close()

    return jsonify(
        teams=result
    )


# =========================================================
# MENTOR: TEAM DETAIL
# =========================================================

@app.get(
    "/api/mentor/team/<int:tid>"
)
def mentor_team_detail(tid):

    user = me()

    if (
        not user
        or user["role"] != "mentor"
    ):

        return jsonify(
            error="Mentor access required."
        ), 403

    c = db()

    team = c.execute(
        """
        SELECT *
        FROM teams
        WHERE id=?
        """,
        (tid,)
    ).fetchone()

    if not team:

        c.close()

        return jsonify(
            error="Team not found."
        ), 404

    pulse = c.execute(
        """SELECT p.*,u.name FROM pulses p JOIN users u ON u.id=p.user_id
           WHERE p.team_id=? ORDER BY p.created_at DESC LIMIT 1""", (tid,)
    ).fetchone()
    members = c.execute(
        """SELECT u.id,u.name,u.email,CASE WHEN u.id=t.owner_id THEN 1 ELSE 0 END is_owner
           FROM team_members tm JOIN users u ON u.id=tm.user_id JOIN teams t ON t.id=tm.team_id
           WHERE tm.team_id=? ORDER BY is_owner DESC,u.name COLLATE NOCASE""", (tid,)
    ).fetchall()
    c.close()
    return jsonify(team=dict(team),members=[dict(r) for r in members],pulse=(dict(pulse) if pulse else None),risk=risk_for(tid))


# =========================================================
# MENTOR: DELETE TEAM
# =========================================================
@app.delete("/api/mentor/team/<int:tid>")
@app.delete("/api/mentor/team/<int:tid>/delete")
def mentor_delete_team(tid):
    user = me()
    if not user or user["role"] != "mentor":
        return jsonify(error="Mentor access required."), 403

    c = db()
    try:
        team = c.execute("SELECT id,name FROM teams WHERE id=?", (tid,)).fetchone()
        if not team:
            return jsonify(error="Team not found."), 404

        # Delete dependent records first. This works with existing databases
        # even though older schemas did not define ON DELETE CASCADE.
        for table in ("mentor_tips", "mentor_messages", "alerts", "pulses", "team_members"):
            try:
                c.execute(f"DELETE FROM {table} WHERE team_id=?", (tid,))
            except sqlite3.OperationalError:
                pass

        c.execute("DELETE FROM teams WHERE id=?", (tid,))
        c.commit()
        return jsonify(ok=True, message=f"{team['name']} deleted successfully.", deleted_team_id=tid)
    except Exception as exc:
        c.rollback()
        return jsonify(error=f"Unable to delete team: {exc}"), 500
    finally:
        c.close()


# =========================================================
# SMART AI SOLUTION ENGINE
# =========================================================

def _fallback_smart_solution(team_name, status, blocker, accomplished, next_step, mentor_tip=""):
    """
    Safe local fallback used only when no AI API key is configured or
    the AI service cannot be reached.

    It intentionally avoids pretending to know a technical cause that
    the team did not report.
    """
    blocker = (blocker or "").strip() or "No blocker was reported."
    accomplished = (accomplished or "").strip() or "Not provided."
    next_step = (next_step or "").strip() or "Not provided."
    mentor_tip = (mentor_tip or "").strip()

    return f"""AI SOLUTION PLAN

Team: {team_name}

CURRENT STATUS
{status}

ACTUAL TEAM REPORT
Blocker: {blocker}
Already completed: {accomplished}
Reported next step: {next_step}
Mentor tip: {mentor_tip or "No mentor tip provided."}

PROBLEM UNDERSTANDING
The current information does not prove a specific technical root cause. The safest approach is to solve the reported blocker directly, preserve completed work, and verify each step before changing unrelated parts of the project.

IMMEDIATE ACTION PLAN
1. Turn the blocker into one concrete, testable task.
2. Assign one primary owner and one support member.
3. Keep already completed work unchanged unless testing shows it is part of the problem.
4. Work on the smallest useful result first and verify it.
5. If the task remains blocked, report the exact error, missing input, dependency, or decision needed.

30-MINUTE TARGET
Produce one concrete result: a working fix, a verified cause, or a clearly documented handoff to the person who can resolve it.

SUCCESS CONDITION
The team can demonstrate what changed, what works now, and who owns the next step.

MENTOR CHECKPOINT
Ask the team to demonstrate the blocker and the result of the first action before recommending a larger change.

NOTE
This is the local fallback plan. When PULSECHECK_AI_API_KEY/OPENAI_API_KEY is configured, PulseCheck uses the AI Mentor Copilot for a situation-specific solution."""


def generate_ai_solution(team_name, status, blocker, accomplished, next_step, mentor_tip=""):
    """
    Real AI Mentor Copilot.

    The model receives the complete team situation and is explicitly told
    not to guess missing facts. This makes it useful for technical,
    coordination, presentation, deadline, deployment, database, AI/ML,
    integration, or other problems without maintaining a giant keyword list.

    Supported environment variables:
      OPENAI_API_KEY or PULSECHECK_AI_API_KEY
      PULSECHECK_AI_MODEL (default: gpt-5.6-luna)
    """
    api_key = (
        os.environ.get("PULSECHECK_AI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()

    if not api_key:
        return _fallback_smart_solution(
            team_name, status, blocker, accomplished, next_step, mentor_tip
        )

    model = os.environ.get("PULSECHECK_AI_MODEL", "gpt-5.6-luna").strip()

    prompt = f"""
You are the AI Mentor Copilot inside PulseCheck, a hackathon/team-progress
platform.

Your job is to solve the team's ACTUAL reported problem. You are not a
keyword classifier and you must not force the situation into a predefined
category.

Analyze the complete situation:
- team name
- current status
- actual blocker
- completed work
- reported next step
- mentor's optional tip

Important rules:
1. Treat the BLOCKER as the primary problem statement.
2. Use completed work as a constraint: do not tell the team to redo work
   that is already completed unless the reported evidence indicates it is
   actually part of the problem.
3. If the problem is technical, give practical debugging/implementation
   steps.
4. If the problem is people/coordination related, give a practical team
   recovery plan.
5. If several problems exist, identify the immediate bottleneck and put
   other work in priority order.
6. Never invent an error message, architecture, technology, team member
   skill, root cause, or completed feature that was not reported.
7. When the exact root cause is unknown, say that it is unknown and give
   the smallest test that will identify it.
8. Do not give generic motivational advice as the main solution.
9. Make the plan realistic for a hackathon team with limited time.
10. Include a fallback/demo-safe approach when the primary fix may take
    too long.
11. The mentor tip is context, not unquestionable truth. Use it when it
    helps, but do not repeat it as a fact if it conflicts with the team
    report.
12. Keep the answer concrete enough that the team can start immediately.

Return ONLY a structured plain-text solution with exactly these sections:

AI SOLUTION PLAN
Team:
Problem understanding:
Problem category:
Current status:
Actual blocker:
Already completed:
Reported next step:
Mentor tip:

ROOT CAUSE / WHAT IS KNOWN
...

PRIORITY
1. ...
2. ...
3. ...

STEP-BY-STEP ACTION
1. ...
2. ...
3. ...
4. ...
5. ...

OWNER / SUPPORT
Primary owner:
Support:
Mentor:

30-MINUTE TARGET
...

SUCCESS CONDITION
...

IF THE FIRST PLAN FAILS
...

MENTOR CHECKPOINT
...

TEAM INPUT
Team: {team_name}
Status: {status}
Blocker: {blocker or "Not provided"}
Completed: {accomplished or "Not provided"}
Next step: {next_step or "Not provided"}
Mentor tip: {mentor_tip or "Not provided"}
""".strip()

    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 1800
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = json.loads(response.read().decode("utf-8"))

        # Responses API commonly exposes output_text directly.
        solution = (body.get("output_text") or "").strip()

        # Robust fallback for responses where output_text is not populated.
        if not solution:
            parts = []
            for item in body.get("output", []) or []:
                for content in item.get("content", []) or []:
                    if isinstance(content, dict) and content.get("text"):
                        parts.append(str(content["text"]))
            solution = "\n".join(parts).strip()

        if not solution:
            raise ValueError("AI returned an empty solution.")

        return solution

    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"[PulseCheck AI] AI service unavailable: {exc}")
        return _fallback_smart_solution(
            team_name, status, blocker, accomplished, next_step, mentor_tip
        )
    except Exception as exc:
        print(f"[PulseCheck AI] Unexpected AI error: {exc}")
        return _fallback_smart_solution(
            team_name, status, blocker, accomplished, next_step, mentor_tip
        )


# Keep this name so any existing frontend/backend code that calls
# generate_smart_solution directly continues to work.
def generate_smart_solution(team_name, status, blocker, accomplished, next_step, mentor_tip=""):
    return generate_ai_solution(
        team_name=team_name,
        status=status,
        blocker=blocker,
        accomplished=accomplished,
        next_step=next_step,
        mentor_tip=mentor_tip
    )


# =========================================================
# GENERATE AI SOLUTION
# =========================================================

@app.post(
    "/api/mentor/generate-solution"
)
def generate_solution():

    user = me()

    if (
        not user
        or user["role"] != "mentor"
    ):

        return jsonify(
            error="Mentor access required."
        ), 403

    data = request.get_json(
        force=True
    )

    team_id = data.get(
        "team_id"
    )

    if not team_id:

        return jsonify(
            error="Team ID is required."
        ), 400

    c = db()

    team = c.execute(
        """
        SELECT *
        FROM teams
        WHERE id=?
        """,
        (team_id,)
    ).fetchone()

    pulse = c.execute(
        """
        SELECT *
        FROM pulses
        WHERE team_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (team_id,)
    ).fetchone()

    # Read the latest mentor tip for this team. This is optional context
    # for the AI and does not change the existing team/pulse workflow.
    mentor_tip_row = c.execute(
        """
        SELECT tip
        FROM mentor_tips
        WHERE team_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (team_id,)
    ).fetchone()

    mentor_tip = mentor_tip_row["tip"] if mentor_tip_row else ""

    c.close()

    if not team:

        return jsonify(
            error="Team not found."
        ), 404

    if not pulse:

        return jsonify(
            error=(
                "This team has not submitted "
                "a pulse yet."
            )
        ), 400

    status_map = {
        "on_track": "On track",
        "at_risk": "Slightly stuck",
        "blocked": "Blocked"
    }

    status = status_map.get(
        pulse["status"],
        pulse["status"]
    )

    solution = generate_smart_solution(

        team_name=team["name"],

        status=status,

        blocker=pulse["blocker"],

        accomplished=pulse["accomplished"],

        next_step=pulse["next_step"],

        mentor_tip=mentor_tip
    )

    return jsonify(

        ok=True,

        team=team["name"],

        category="AI Solution",

        solution=solution

    )


# =========================================================
# SEND SOLUTION
# =========================================================

@app.post(
    "/api/mentor/send-solution"
)
def send_solution():

    user = me()

    if (
        not user
        or user["role"] != "mentor"
    ):

        return jsonify(
            error="Mentor access required."
        ), 403

    data = request.get_json(
        force=True
    )

    team_id = data.get(
        "team_id"
    )

    solution = data.get(
        "solution",
        ""
    ).strip()

    if not team_id:

        return jsonify(
            error="Team ID is required."
        ), 400

    if not solution:

        return jsonify(
            error="Solution cannot be empty."
        ), 400

    c = db()

    team = c.execute(
        """
        SELECT id,name
        FROM teams
        WHERE id=?
        """,
        (team_id,)
    ).fetchone()

    if not team:

        c.close()

        return jsonify(
            error="Team not found."
        ), 404

    now = datetime.utcnow().isoformat()

    c.execute(
        """
        INSERT INTO mentor_messages
        (
            team_id,
            mentor_id,
            solution,
            created_at,
            seen
        )
        VALUES(?,?,?,?,0)
        """,
        (
            team_id,
            user["id"],
            solution,
            now
        )
    )

    message_id = c.execute(
        """
        SELECT last_insert_rowid() id
        """
    ).fetchone()["id"]

    c.commit()
    c.close()

    return jsonify(

        ok=True,

        message=(
            "Solution sent to "
            f"{team['name']} successfully."
        ),

        message_id=message_id

    )


# =========================================================
# TEAM RECEIVES SOLUTION
# =========================================================

@app.get(
    "/api/team/mentor-solutions"
)
def team_mentor_solutions():

    user = me()

    if not user:

        return jsonify(
            error="Login required."
        ), 401

    if user["role"] != "team":

        return jsonify(
            error="Team access required."
        ), 403

    team = my_team(
        user["id"]
    )

    if not team:

        return jsonify(
            solutions=[]
        )

    c = db()

    rows = c.execute(
        """
        SELECT
            mm.id,
            mm.solution,
            mm.created_at,
            mm.seen,
            u.name AS mentor_name
        FROM mentor_messages mm
        JOIN users u
            ON u.id=mm.mentor_id
        WHERE mm.team_id=?
        ORDER BY mm.created_at DESC
        LIMIT 20
        """,
        (team["id"],)
    ).fetchall()

    c.close()

    return jsonify(

        team=team,

        solutions=[
            dict(row)
            for row in rows
        ]

    )


@app.post(
    "/api/team/mentor-solutions/<int:message_id>/seen"
)
def mark_solution_seen(message_id):

    user = me()

    if (
        not user
        or user["role"] != "team"
    ):

        return jsonify(
            error="Team access required."
        ), 403

    team = my_team(
        user["id"]
    )

    if not team:

        return jsonify(
            error="No team found."
        ), 404

    c = db()

    c.execute(
        """
        UPDATE mentor_messages
        SET seen=1
        WHERE id=?
        AND team_id=?
        """,
        (
            message_id,
            team["id"]
        )
    )

    c.commit()
    c.close()

    return jsonify(
        ok=True
    )


# =========================================================
# TEAM MEMBERS
# =========================================================
@app.get("/api/team/members")
def team_members():
    user = me()
    if not user or user["role"] != "team":
        return jsonify(error="Team access required."), 403
    team = my_team(user["id"])
    if not team:
        return jsonify(team=None, members=[])
    c = db()
    rows = c.execute("""
        SELECT u.id,u.name,u.email,
               CASE WHEN u.id=t.owner_id THEN 1 ELSE 0 END is_owner
        FROM team_members tm
        JOIN users u ON u.id=tm.user_id
        JOIN teams t ON t.id=tm.team_id
        WHERE tm.team_id=?
        ORDER BY is_owner DESC,u.name COLLATE NOCASE
    """, (team["id"],)).fetchall()
    c.close()
    return jsonify(team=team, members=[dict(r) for r in rows])

# =========================================================
# MENTOR TIPS
# =========================================================
@app.get("/api/team/mentor-tips")
def team_mentor_tips():
    user = me()
    if not user or user["role"] != "team":
        return jsonify(error="Team access required."), 403
    team = my_team(user["id"])
    if not team:
        return jsonify(tips=[])
    c = db()
    rows = c.execute("""
        SELECT mt.id,mt.tip,mt.created_at,mt.seen,u.name mentor_name
        FROM mentor_tips mt JOIN users u ON u.id=mt.mentor_id
        WHERE mt.team_id=? ORDER BY mt.created_at DESC LIMIT 20
    """, (team["id"],)).fetchall()
    c.close()
    return jsonify(tips=[dict(r) for r in rows])

@app.post("/api/team/mentor-tips/<int:tip_id>/seen")
def seen_tip(tip_id):
    user=me()
    if not user or user["role"]!="team": return jsonify(error="Team access required."),403
    team=my_team(user["id"])
    if not team: return jsonify(error="No team found."),404
    c=db(); c.execute("UPDATE mentor_tips SET seen=1 WHERE id=? AND team_id=?",(tip_id,team["id"])); c.commit(); c.close()
    return jsonify(ok=True)

@app.post("/api/mentor/send-tip")
def send_tip():
    user=me()
    if not user or user["role"]!="mentor": return jsonify(error="Mentor access required."),403
    data=request.get_json(force=True); team_id=data.get("team_id"); tip=(data.get("tip") or "").strip()
    if not team_id or not tip: return jsonify(error="Team and tip are required."),400
    c=db(); team=c.execute("SELECT id FROM teams WHERE id=?",(team_id,)).fetchone()
    if not team: c.close(); return jsonify(error="Team not found."),404
    cur=c.execute("INSERT INTO mentor_tips(team_id,mentor_id,tip,created_at,seen) VALUES(?,?,?,?,0)",(team_id,user["id"],tip,datetime.utcnow().isoformat())); c.commit(); tid=cur.lastrowid; c.close()
    return jsonify(ok=True,tip_id=tid)

# =========================================================
# ALERTS
# =========================================================

@app.get("/api/alerts")
def alerts():

    user = me()

    if (
        not user
        or user["role"] != "mentor"
    ):

        return jsonify(
            error="Mentor access required."
        ), 403

    c = db()

    rows = c.execute(
        """
        SELECT
            a.*,
            t.name AS team
        FROM alerts a
        JOIN teams t
            ON t.id=a.team_id
        ORDER BY a.created_at DESC
        LIMIT 30
        """
    ).fetchall()

    c.close()

    return jsonify(
        alerts=[
            dict(row)
            for row in rows
        ]
    )


@app.post(
    "/api/alerts/<int:aid>/seen"
)
def seen(aid):

    user = me()

    if (
        not user
        or user["role"] != "mentor"
    ):

        return jsonify(
            error="Mentor access required."
        ), 403

    c = db()

    c.execute(
        """
        UPDATE alerts
        SET seen=1
        WHERE id=?
        """,
        (aid,)
    )

    c.commit()
    c.close()

    return jsonify(
        ok=True
    )


# =========================================================
# ANALYTICS
# =========================================================

@app.get("/api/analytics")
def analytics():

    user = me()

    if not user:

        return jsonify(
            error="Login required."
        ), 401

    c = db()

    if user["role"] == "mentor":

        total = c.execute(
            "SELECT COUNT(*) n FROM teams"
        ).fetchone()["n"]

        pulse_count = c.execute(
            "SELECT COUNT(*) n FROM pulses"
        ).fetchone()["n"]

        rows = c.execute(
            """
            SELECT status,COUNT(*) n
            FROM pulses
            GROUP BY status
            """
        ).fetchall()

        c.close()

        values = {
            row["status"]: row["n"]
            for row in rows
        }

        return jsonify(

            teams=total,

            pulses=pulse_count,

            on_track=values.get(
                "on_track",
                0
            ),

            at_risk=values.get(
                "at_risk",
                0
            ),

            blocked=values.get(
                "blocked",
                0
            )

        )

    team = my_team(
        user["id"]
    )

    if not team:

        c.close()

        return jsonify(
            error="No team found."
        ), 404

    rows = c.execute(
        """
        SELECT status,COUNT(*) n
        FROM pulses
        WHERE team_id=?
        GROUP BY status
        """,
        (team["id"],)
    ).fetchall()

    c.close()

    values = {
        row["status"]: row["n"]
        for row in rows
    }

    return jsonify(

        on_track=values.get(
            "on_track",
            0
        ),

        at_risk=values.get(
            "at_risk",
            0
        ),

        blocked=values.get(
            "blocked",
            0
        )

    )


# =========================================================
# RECOMMENDATION
# =========================================================

@app.get("/api/recommendation")
def recommendation():

    user = me()

    if not user:

        return jsonify(
            error="Login required."
        ), 401

    team = my_team(
        user["id"]
    )

    if not team:

        return jsonify(
            recommendation=(
                "Join a team and submit "
                "your first pulse."
            )
        )

    risk = risk_for(
        team["id"]
    )

    if risk["risk"] == "critical":

        message = (
            "Mentor intervention recommended now. "
            "Review the team's blocker and convert "
            "it into one small testable task."
        )

    elif risk["risk"] == "warning":

        message = (
            "Keep momentum. Resolve the blocker, "
            "complete the next concrete step, and "
            "submit another pulse."
        )

    else:

        message = (
            "Team is healthy. Keep the next step "
            "small, specific, and demo-focused."
        )

    return jsonify(

        recommendation=message,

        risk_score=risk["score"]

    )


# Vercel imports the Flask application instead of executing this file as
# __main__, so initialize the SQLite schema/seeds during module import.
# Local development still uses the normal __main__ block below.
if os.environ.get("VERCEL"):
    init_db()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    init_db()

    print("")
    print("======================================")
    print("        PULSECHECK RUNNING")
    print("======================================")
    print("")
    print("Local:   http://127.0.0.1:5000")
    print("Network: http://YOUR-IP:5000")
    print("")
    print("Mentor:")
    print("mentor@pulsecheck.demo")
    print("mentor123")
    print("")
    print("Team:")
    print("team@pulsecheck.demo")
    print("team123")
    print("======================================")
    print("")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )