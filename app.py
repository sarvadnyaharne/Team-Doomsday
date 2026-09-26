from flask import Flask, request, jsonify, session, render_template, redirect
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime, timedelta


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "pulsecheck-demo-secret"
)

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

def generate_smart_solution(team_name, status, blocker, accomplished, next_step):
    """Deterministic, context-first mentor copilot.

    This intentionally does not invent technical failures. The first
    matching rule is the actual blocker/question reported by the team,
    then status/completed work/next step refine the plan.
    """
    raw_blocker = (blocker or "").strip()
    raw_done = (accomplished or "").strip()
    raw_next = (next_step or "").strip()

    blocker_text = raw_blocker or "No blocker was reported."
    done_text = raw_done or "Not provided."
    next_text = raw_next or "Not provided."
    text = f"{blocker_text} {next_text}".lower().strip()

    category = "Team Progress"
    diagnosis = "The team has not described a specific technical failure. The plan therefore starts from the exact situation reported instead of assuming a different problem."
    actions = [
        f"Restate the immediate goal in one sentence: {next_text}.",
        "Choose one person to own the next action and one person to support them.",
        "Work only on that action for the next 30 minutes.",
        "Record the result as DONE, BLOCKED, or NEEDS HELP.",
        "If blocked again, report the exact missing input or decision to the mentor."
    ]
    target = "One clearly defined next action is completed or reaches a documented handoff."
    success = "The team can point to a concrete result and a clear next owner."

    # 1) Human conversation / coordination — must be checked before generic UI terms.
    if any(k in text for k in [
        "start a quick team conversation", "quick team conversation",
        "team conversation", "start conversation", "how do i talk",
        "how do i communicate", "standup", "quick meeting", "team meeting",
        "not cooperating", "not responding", "coordination", "communication",
        "conflict between", "members are not", "member is not"
    ]):
        category = "Team Coordination / Communication"
        diagnosis = "The reported need is a team conversation or coordination step, not a product bug. The immediate goal is to create a short, structured conversation that ends with ownership."
        actions = [
            "Open the team's shared chat or call and post: '10-minute sync — blocker, owner, next action.'",
            "Give each member 30 seconds to state: DONE, BLOCKED, and what they can finish next.",
            "Pick one immediate deliverable and name exactly one owner for it.",
            "Ask anyone blocked to state the exact help they need and who can provide it.",
            "End by writing the final owner + next action + 30-minute target in the shared chat."
        ]
        target = "A 10-minute team sync ends with one owner and one concrete 30-minute task."
        success = "Every active member knows what they own next, and the reported coordination issue has a specific follow-up."

    # 2) Presentation / pitch / demo.
    elif any(k in text for k in ["ppt", "presentation", "slides", "pitch", "demo", "viva"]):
        category = "Presentation / Demo"
        diagnosis = "The reported work is presentation or demonstration focused. The fastest path is to freeze scope and make the existing product easy to explain and demonstrate."
        actions = [
            "Freeze new features for this work session and list only the screens needed for the demo.",
            "Assign one owner for slides, one for the live demo, and one for fact-checking.",
            "Build the story as problem → solution → workflow → technology → impact.",
            "Run the exact demo once from login to the final result without changing the flow.",
            "Keep screenshots of the critical screens as a fallback for the presentation."
        ]
        target = "A complete first-pass pitch and one repeatable end-to-end demo are ready."
        success = "The team can explain the problem clearly and demonstrate the core workflow without scope changes."

    # 3) Authentication / team access.
    elif any(k in text for k in ["login", "log in", "sign in", "password", "account", "join team", "create team", "team access"]):
        category = "Authentication / Team Access"
        diagnosis = "The report concerns entry or team membership. Verify session and membership state first; do not change unrelated dashboard components."
        actions = [
            "Reproduce the exact login or team-access flow with one test account.",
            "Confirm the login request creates the expected session user.",
            "Confirm the user is either creating one team or joining the selected existing team.",
            "For joining, verify the team row and team_members row exist after the request.",
            "Refresh the dashboard and verify the team name and member list use the same team id."
        ]
        target = "The test account reaches exactly one intended team and its membership is visible after refresh."
        success = "The correct team name and members appear without creating an unintended second team."

    # 4) Database/data.
    elif any(k in text for k in ["database", "sqlite", "sql", "schema", "table", "query", "data not", "data missing"]):
        category = "Database / Data"
        diagnosis = "The blocker points to stored data. The correct first move is to verify the affected record and query rather than redesigning the application."
        actions = [
            "Identify the exact table and record the current screen expects.",
            "Check that the required columns exist in the current SQLite schema.",
            "Run the smallest read/write test with one known team or user.",
            "Fix only the schema or query mismatch found by that test.",
            "Repeat the original user flow and verify the saved record is returned."
        ]
        target = "The affected record can be written and read back correctly with known test data."
        success = "The original screen shows the expected stored data after a fresh request."

    # 5) Backend/API.
    elif any(k in text for k in ["backend", "api", "server", "flask", "endpoint", "request", "response", "500", "404"]):
        category = "Backend / API"
        diagnosis = "The blocker explicitly names the backend/API layer. Isolate the single request involved and fix that contract before touching the rest of the UI."
        actions = [
            "Identify the exact endpoint used by the affected screen.",
            "Call that endpoint with one known input and record its status code.",
            "Fix the first server-side exception or validation error returned by that request.",
            "Verify the JSON field names match what the current frontend reads.",
            "Repeat the complete user flow once the endpoint returns the expected response."
        ]
        target = "The affected endpoint returns the expected JSON response for one real test request."
        success = "The original frontend action completes using the verified backend response."

    # 6) Frontend/UI/browser.
    elif any(k in text for k in ["frontend", "ui", "screen", "button", "page", "display", "visible", "loading", "javascript", "browser", "click"]):
        category = "Frontend / UI"
        diagnosis = "The blocker explicitly concerns the user-facing interface. Reproduce the exact interaction and inspect the first observable UI failure before changing other screens."
        actions = [
            "Open the exact screen and reproduce the reported interaction once.",
            "Check the browser console and Network tab for the first failed request or JavaScript error.",
            "Verify the element id, event handler, endpoint and returned field names used by that screen.",
            "Fix only the affected component and keep the existing layout unchanged.",
            "Repeat the full user flow from the first click to the expected visible result."
        ]
        target = "The exact reported UI action reaches its expected visible result without a console or network error."
        success = "The user can complete the affected action from the current screen without changing unrelated UI."

    # 7) Deadline / time pressure.
    elif any(k in text for k in ["deadline", "time left", "hours left", "tomorrow", "urgent", "late", "behind schedule"]):
        category = "Deadline / Prioritization"
        diagnosis = "The report is schedule-related. The immediate response is to reduce scope to the smallest demonstrable outcome and assign ownership."
        actions = [
            "List the remaining work and mark each item CORE, SUPPORTING, or OPTIONAL.",
            "Freeze OPTIONAL work until the core workflow is stable.",
            "Assign one owner to each CORE item and identify dependencies.",
            "Run a short integration check after the next core item is completed.",
            "Keep a backup demo path using screenshots or a recorded flow for fragile steps."
        ]
        target = "The core user journey is stable and the remaining work has clear owners."
        success = "The team can demonstrate the core outcome without depending on unfinished optional features."

    # 8) Resource / skill / dependency.
    elif any(k in text for k in ["need a", "need help", "expert", "resource", "no one knows", "dependency", "someone to"]):
        category = "Resource / Skill Dependency"
        diagnosis = "The team reports a missing person, skill, or dependency. The useful next step is to define the smallest handoff needed rather than waiting for a full replacement."
        actions = [
            "State the exact task that is blocked and the skill/input it requires.",
            "Break that task into a small handoff that another member or mentor can review.",
            "Ask for one specific piece of help rather than general assistance.",
            "Continue independent work that does not depend on the missing input.",
            "Set a checkpoint to merge the help back into the main workflow."
        ]
        target = "The dependency is reduced to one concrete handoff with an owner and checkpoint."
        success = "The team either receives the required input or has a documented workaround."

    return f"""AI SOLUTION PLAN
Team: {team_name}

PROBLEM CATEGORY
{category}

TEAM STATUS
{status}

WHAT THE TEAM REPORTED
Blocker: {blocker_text}
Already completed: {done_text}
Reported next step: {next_text}

AI DIAGNOSIS
{diagnosis}

WHAT TO DO NOW
1. {actions[0]}
2. {actions[1]}
3. {actions[2]}
4. {actions[3]}
5. {actions[4]}

30-MINUTE TARGET
{target}

SUCCESS CONDITION
{success}

MENTOR CHECKPOINT
After the first action, ask the team to report the observable result before changing the plan.

WHY THIS PLAN IS SPECIFIC
The plan is anchored to the team's exact blocker first, then refined with the reported status, completed work and next step. It does not invent a technical failure when the team has reported a communication, planning or other non-technical need."""


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

        next_step=pulse["next_step"]
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
