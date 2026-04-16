import uuid
import secrets
import json as _json
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

IC_DISTRICT_SEARCH = "https://mobile.infinitecampus.com/api/district/searchDistrict"

# Flask-session-id -> { session: requests.Session, courses: list | None,
#                       base: str, app_name: str }
STORE = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def _sid():
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    return session["sid"]


def _state():
    return STORE.get(_sid())


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _current_semester_range():
    """Hardcoded fallback semester range based on local date."""
    now = datetime.now()
    if now.month >= 8 or (now.month == 1 and now.day <= 5):
        start_year = now.year if now.month >= 8 else now.year - 1
        end_year   = start_year + 1
        return f"{start_year}-08-01", f"{end_year}-01-05", "Semester 1"
    else:
        return f"{now.year}-01-06", f"{now.year}-07-31", "Semester 2"


def _resolve_semester_range(ic_terms):
    """
    Use the real term dates from IC to determine which semester we're in
    and return (start_iso, end_iso, label) for the full semester window.

    Dublin USD has 4 terms per year: Q1, Semester 1, Q3, Semester 2.
    The semester GRADE covers both the quarter and the semester term, so:
      Fall window  = earliest(Q1, S1).start → latest(Q1, S1).end
      Spring window = earliest(Q3, S2).start → latest(Q3, S2).end

    We split terms into fall (starts Jul–Dec) vs spring (starts Jan–Jun),
    then pick whichever window contains today. If today is between semesters,
    we pick the nearest one. Falls back to _current_semester_range() if IC
    term data is missing or unparseable.
    """
    from datetime import date as _date

    today = datetime.now().date()

    def _parse(s):
        if not s:
            return None
        try:
            return _date.fromisoformat(str(s)[:10])
        except Exception:
            return None

    # Sort terms by sequence number
    sorted_terms = sorted(
        [t for t in (ic_terms or []) if isinstance(t, dict)],
        key=lambda t: t.get("seq") or t.get("termSeq") or 0,
    )
    if not sorted_terms:
        return _current_semester_range()

    # Split into fall half (start month >= 7) and spring half (start month < 7)
    fall_terms   = [t for t in sorted_terms
                    if _parse(t.get("startDate")) and _parse(t.get("startDate")).month >= 7]
    spring_terms = [t for t in sorted_terms
                    if _parse(t.get("startDate")) and _parse(t.get("startDate")).month < 7]

    def _half_range(terms):
        starts = [_parse(t.get("startDate")) for t in terms]
        ends   = [_parse(t.get("endDate"))   for t in terms]
        starts = [d for d in starts if d]
        ends   = [d for d in ends   if d]
        if not starts or not ends:
            return None, None
        return min(starts), max(ends)

    f_start, f_end = _half_range(fall_terms)
    s_start, s_end = _half_range(spring_terms)

    # Pick whichever window contains today
    if f_start and f_end and f_start <= today <= f_end:
        return str(f_start), str(f_end), "Semester 1"
    if s_start and s_end and s_start <= today <= s_end:
        return str(s_start), str(s_end), "Semester 2"

    # Today is outside all term windows — pick the nearest upcoming, or most recent
    if f_start and s_start:
        # Both halves exist — pick whichever end date is closest to today
        f_dist = abs((today - f_end).days) if f_end else 99999
        s_dist = abs((today - s_end).days) if s_end else 99999
        if f_dist <= s_dist:
            return str(f_start), str(f_end), "Semester 1"
        else:
            return str(s_start), str(s_end), "Semester 2"

    # Only one half found
    if f_start:
        return str(f_start), str(f_end), "Semester 1"
    if s_start:
        return str(s_start), str(s_end), "Semester 2"

    return _current_semester_range()


# ── auth ─────────────────────────────────────────────────────────────────────

def ic_login(username, password, base_url, app_name):
    """
    Authenticate against any Infinite Campus instance.
    base_url  : e.g. "https://icampus.dublinusd.org/campus"
    app_name  : e.g. "dublin"
    """
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    # Ensure /campus path segment is present
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    if not parsed.path or parsed.path == "/":
        base_url = base_url + "/campus"
    app_name   = app_name or base_url.split("/")[-1] or "campus"
    login_page = f"{base_url}/portal/students/{app_name}.jsp"
    login_post = f"{base_url}/verify.jsp"
    grades_api = f"{base_url}/resources/portal/grades"

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"{base_url}/portal/students/{app_name}",
    })
    try:
        s.get(login_page, timeout=20)
    except Exception:
        pass

    r = s.post(login_post, data={
        "username":        username,
        "password":        password,
        "appName":         app_name,
        "url":             "nav-wrapper",
        "lang":            "en",
        "portalLoginPage": "students",
        "portalUrl":       f"portal/students/{app_name}.jsp",
    }, timeout=20, allow_redirects=True)

    text = r.text.lower()
    if "password-error" in text or ("invalid" in text and "password" in text):
        return None, None, None, "Invalid username or password."
    if 'name="password"' in r.text or 'type="password"' in r.text:
        return None, None, None, "Invalid username or password."

    # Confirm auth by probing the grades API
    probe = s.get(grades_api, timeout=20)
    if probe.status_code in (401, 403):
        return None, None, None, "Login failed — credentials rejected by server."
    if probe.status_code != 200:
        return None, None, None, f"Grades API unreachable after login (HTTP {probe.status_code})."
    try:
        probe.json()
    except Exception:
        return None, None, None, "Grades API returned unexpected non-JSON response."

    return s, base_url, app_name, None


# ── grade fetching ────────────────────────────────────────────────────────────

def _best_task(grading_tasks):
    """
    Pick the single most-current grading task from a course's gradingTask list.
    Priority: Semester Grade with progressPercent > Term Grade > Progress Grade.
    """
    for want in ("Semester Grade", "Term Grade", "Progress Grade"):
        for t in grading_tasks:
            if t.get("taskName", "") == want:
                if t.get("progressPercent") is not None or t.get("percent") is not None:
                    return t
    # Fallback: first task that has any percent
    for t in grading_tasks:
        if t.get("progressPercent") is not None or t.get("percent") is not None:
            return t
    return None


def fetch_grades(s: requests.Session, base_url=None, app_name=None):
    """
    Returns (courses, error_str, raw_str).
    courses is a list of dicts; error_str/raw_str are set on failure.

    IC API: /campus/resources/portal/grades
    Response shape:
      [ { enrollmentID, terms: [ { termID, termName, termSeq, courses: [ {
              sectionID, courseName, teacherDisplay, sectionNumber,
              gradingTasks: [ { taskName, score, percent,
                                progressScore, progressPercent,
                                progressPointsEarned, progressTotalPoints,
                                hasDetail, hasAssignments, scoreID } ]
          } ] } ],
          courses: [ ... ]   ← same courses but all terms flattened
      } ]
    """
    grades_api = f"{base_url}/resources/portal/grades" if base_url else \
                 "https://icampus.dublinusd.org/campus/resources/portal/grades"
    r = s.get(grades_api, timeout=20)
    if r.status_code != 200:
        return None, f"Grades API returned HTTP {r.status_code}.", r.text[:4000]
    try:
        data = r.json()
    except Exception:
        return None, "Grades API did not return JSON.", r.text[:4000]

    if not isinstance(data, list) or not data:
        return [], None, None

    enrollment = data[0]  # single student

    # ── Pull personID + calendarID from the grading task data ──
    person_id   = None
    calendar_id = enrollment.get("calendarID")
    for term in enrollment.get("terms", []):
        for course in term.get("courses", []):
            for task in course.get("gradingTasks", []):
                if task.get("personID"):
                    person_id = task["personID"]
                if task.get("calendarID") and not calendar_id:
                    calendar_id = task["calendarID"]
                if person_id and calendar_id:
                    break
            if person_id:
                break
        if person_id:
            break

    # ── Fetch ALL assignment marks for this student in one call ──
    # Endpoint: /campus/resources/section/teacherSections/assignmentMark
    # Returns a flat list of mark objects; we index them by assignmentID.
    marks_by_id = {}
    if person_id and calendar_id:
        try:
            _base = (base_url or "https://icampus.dublinusd.org/campus").rstrip("/")
            rm = s.get(
                f"{_base}/resources/section/teacherSections/assignmentMark",
                params={"_calendarID": calendar_id, "_personID": person_id},
                timeout=20,
            )
            if rm.status_code == 200:
                marks_list = rm.json()
                if isinstance(marks_list, list):
                    for m in marks_list:
                        aid = m.get("assignmentID") or m.get("_id")
                        if aid is not None:
                            marks_by_id[aid] = m
                elif isinstance(marks_list, dict):
                    # some versions wrap in {data: [...]}
                    for m in marks_list.get("data", []):
                        aid = m.get("assignmentID") or m.get("_id")
                        if aid is not None:
                            marks_by_id[aid] = m
        except Exception:
            pass  # marks unavailable; assignments will show without scores

    # ── Walk terms newest-first; build one entry per unique sectionID ──
    terms_raw = sorted(
        enrollment.get("terms", []),
        key=lambda t: t.get("termSeq", 0),
        reverse=True,   # newest first
    )

    seen = set()
    courses = []

    for term in terms_raw:
        term_name  = term.get("termName", "")
        term_seq   = term.get("termSeq", 0)
        term_start = term.get("startDate", "2025-08-01")
        term_end   = term.get("endDate",   "2026-06-30")

        for course in term.get("courses", []):
            sec_id = course.get("sectionID")
            if sec_id in seen:
                continue  # already have the most-recent grade for this course

            task = _best_task(course.get("gradingTasks", []))
            if task is None:
                continue  # no grade yet — skip until a graded term appears

            seen.add(sec_id)
            pct    = _num(task.get("progressPercent")) or _num(task.get("percent"))
            letter = task.get("progressScore") or task.get("score") or ""
            earned = _num(task.get("progressPointsEarned"))
            total  = _num(task.get("progressTotalPoints"))
            score_id    = task.get("scoreID")
            has_detail  = task.get("hasDetail", False)

            courses.append({
                "section_id":   sec_id,
                "name":         course.get("courseName", "Unknown Course"),
                "teacher":      course.get("teacherDisplay", ""),
                "section":      str(course.get("sectionNumber", "")),
                "current_term": term_name,
                "term_start":   term_start,
                "term_end":     term_end,
                "percent":      pct,
                "letter":       letter,
                "earned":       earned,    # total points earned (from IC's own calc)
                "total":        total,     # total points possible
                "score_id":     score_id,
                "has_detail":   has_detail,
                "assignments":  [],
                "categories":   [],
                "term_history": [],        # filled below
            })

    # ── Build per-course term history (all terms, newest first) ──
    # Index by section_id for fast lookup
    course_map = {c["section_id"]: c for c in courses}

    for term in terms_raw:
        term_name = term.get("termName", "")
        term_seq  = term.get("termSeq", 0)
        for course in term.get("courses", []):
            sec_id = course.get("sectionID")
            if sec_id not in course_map:
                continue
            task = _best_task(course.get("gradingTasks", []))
            if task is None:
                continue
            pct    = _num(task.get("progressPercent")) or _num(task.get("percent"))
            letter = task.get("progressScore") or task.get("score") or ""
            earned = _num(task.get("progressPointsEarned"))
            total  = _num(task.get("progressTotalPoints"))
            course_map[sec_id]["term_history"].append({
                "name":   term_name,
                "seq":    term_seq,
                "pct":    pct,
                "letter": letter,
                "earned": earned,
                "total":  total,
            })

    # Pre-compute the semester window from IC's real term dates
    all_ic_terms = []
    for term in terms_raw:
        all_ic_terms.append({
            "seq":       term.get("termSeq", 0),
            "startDate": term.get("startDate"),
            "endDate":   term.get("endDate"),
            "termName":  term.get("termName", ""),
        })

    # ── Fetch individual assignments for every course ──
    for c in courses:
        assignments, categories = _fetch_assignments(
            s, c, marks_by_id,
            calendar_id=calendar_id,
            person_id=person_id,
            ic_terms=all_ic_terms,
            base_url=base_url,
            app_name=app_name,
        )
        c["assignments"] = assignments
        c["categories"]  = categories

    return courses, None, None


def _parse_detail_response(body, marks_by_id):
    """
    Parse IC's assignment detail response into (assignments, categories).

    Handles three shapes:

    Shape A — Dublin USD (resources/portal/grades/detail/{scoreID}):
      { details: [{ categories: [{ groupID, name, weight,
          assignments: [{ objectSectionID, assignmentName, totalPoints,
                          score, scorePoints, scorePercentage, dueDate,
                          missing, late }] }] }] }

    Shape B — Prism / modern IC:
      { data: [{ categoryID, categoryName, weight,
          assignments: [{ assignmentID, assignmentName, totalPoints,
                          score:{points,percent}, dueDate }] }] }

    Shape C — Legacy Task[]:
      { Task: [{ taskName, weight,
          Assignments: [{ assignmentName, totalPoints, score, dueDate }] }] }

    Returns (assignments, categories) or None if structure not recognised.
    """
    # ── Shape A: Dublin USD details[] wrapper ─────────────────────────────────
    if isinstance(body, dict) and body.get("details"):
        details = body["details"]
        if not isinstance(details, list):
            return None
        # Merge categories across all detail objects (one per term)
        cat_order  = []   # insertion-ordered list of canonical names
        cat_map    = {}   # lower(name) → { name, weight, assignments[] }
        for detail in details:
            if not isinstance(detail, dict):
                continue
            for cat in (detail.get("categories") or []):
                if not isinstance(cat, dict):
                    continue
                cname  = (cat.get("name") or cat.get("categoryName") or "Category").strip()
                weight = _num(cat.get("weight"))
                key    = cname.lower()
                if key not in cat_map:
                    cat_map[key] = {"name": cname, "weight": weight, "asgns": []}
                    cat_order.append(key)
                for a in (cat.get("assignments") or cat.get("Assignments") or []):
                    if not isinstance(a, dict):
                        continue
                    # Dublin USD uses objectSectionID as assignment identifier
                    aid   = (a.get("objectSectionID") or a.get("assignmentID") or
                             a.get("id") or a.get("_id"))
                    aname = a.get("assignmentName") or a.get("name") or "Assignment"
                    total = _num(a.get("totalPoints") or a.get("pointsPossible"))
                    date  = a.get("dueDate") or a.get("assignedDate") or ""
                    # score: scalar string or number — use explicit None checks
                    # so a legitimate score of 0 is never dropped by Python's `or`
                    _sp = a.get("scorePoints")
                    earned = _num(_sp) if _sp is not None else _num(a.get("score"))
                    if earned is None and marks_by_id and aid is not None:
                        mark = (marks_by_id.get(aid) or marks_by_id.get(str(aid)) or {})
                        for _k in ("score", "scorePoints", "earnedPoints"):
                            _v = mark.get(_k)
                            if _v is not None:
                                earned = _num(_v)
                                if earned is not None:
                                    break
                    cat_map[key]["asgns"].append({
                        "id": aid, "name": aname, "earned": earned,
                        "total": total, "category": cname, "weight": weight,
                        "date": str(date)[:10], "hypothetical": False,
                    })
        if not cat_order:
            return None
        categories  = [{"name": cat_map[k]["name"], "weight": cat_map[k]["weight"]}
                       for k in cat_order]
        assignments = []
        for k in cat_order:
            assignments.extend(cat_map[k]["asgns"])
        assignments.sort(key=lambda x: x["date"], reverse=True)
        return assignments, categories

    # ── Shape B / C: data[] or Task[] (categories-first, assignments nested) ──
    if isinstance(body, dict):
        inner = (body.get("data") or body.get("categories") or
                 body.get("Task") or body.get("tasks") or [])
    elif isinstance(body, list):
        inner = body
    else:
        return None

    if not inner or not isinstance(inner, list):
        return None

    first = inner[0] if inner else {}
    if not isinstance(first, dict):
        return None
    if "assignments" not in first and "Assignments" not in first:
        return None

    categories  = []
    assignments = []

    for item in inner:
        if not isinstance(item, dict):
            continue
        cat_name = (item.get("categoryName") or item.get("taskName") or
                    item.get("name") or f"Category {len(categories)+1}").strip()
        weight   = _num(item.get("weight"))
        categories.append({"name": cat_name, "weight": weight})

        for a in (item.get("assignments") or item.get("Assignments") or []):
            if not isinstance(a, dict):
                continue
            aid   = (a.get("assignmentID") or a.get("objectSectionID") or
                     a.get("id") or a.get("_id"))
            name  = a.get("assignmentName") or a.get("name") or "Assignment"
            total = _num(a.get("totalPoints") or a.get("pointsPossible"))
            date  = a.get("dueDate") or a.get("assignedDate") or ""

            score_obj = a.get("score")
            earned = None
            if isinstance(score_obj, dict):
                earned = _num(score_obj.get("points"))
                if earned is None and total:
                    pct = _num(score_obj.get("percent"))
                    if pct is not None:
                        earned = round(pct / 100 * total, 4)
            if earned is None:
                earned = _num(a.get("scorePoints"))
            if earned is None and not isinstance(score_obj, dict):
                earned = _num(score_obj)
            if earned is None and marks_by_id and aid is not None:
                mark = (marks_by_id.get(aid) or marks_by_id.get(str(aid)) or {})
                for _k in ("score", "scorePoints", "earnedPoints"):
                    _v = mark.get(_k)
                    if _v is not None:
                        earned = _num(_v)
                        if earned is not None:
                            break

            assignments.append({
                "id": aid, "name": name, "earned": earned, "total": total,
                "category": cat_name, "weight": weight,
                "date": str(date)[:10], "hypothetical": False,
            })

    if not categories:
        return None
    assignments.sort(key=lambda x: x["date"], reverse=True)
    return assignments, categories


def _fetch_assignments(s, course, marks_by_id=None, calendar_id=None, person_id=None, ic_terms=None, base_url=None, app_name=None):
    """
    Fetch assignments with their categories for one course section.

    Strategy (in priority order):
      1. resources/portal/grades/detail/{scoreID} — Dublin USD specific; uses
         the scoreID from the grading task, NOT sectionID.
      2. prism/api/portal/grades/assignmentDetail — modern IC.
      3. resources/portal/assignment — legacy fallback.
      4. groupActivity endpoint — maps groupActivityID→categoryID so byDateRange
         assignments can be placed in the right category bucket.
      5. byDateRange + separate categories — last resort; matches by name.
    """
    sec_id   = course["section_id"]
    score_id = course.get("score_id")
    # base_url from the district API already ends with /campus
    # (e.g. "https://icampus.dublinusd.org/campus"). All our path strings
    # below start with /campus/... so we strip it here to avoid doubling up.
    _raw = (base_url or "https://icampus.dublinusd.org/campus").rstrip("/")
    BASE = _raw[:-7] if _raw.endswith("/campus") else _raw
    app_name = app_name or "dublin"

    # ── Supplemental categories (always fetch — gives us empty categories too) ──
    # We'll merge these in as a safety net regardless of which path succeeds.
    sup_cats  = {}   # canonical_name → weight  (insertion order = IC display order)
    sup_lower = {}   # lower(name) → canonical_name
    cat_by_id = {}   # categoryID (int or str) → canonical_name
    try:
        rc = s.get(
            f"{BASE}/campus/api/campus/grading/categories",
            params={"sectionID": sec_id},
            timeout=15,
        )
        if rc.status_code == 200:
            cats_raw = rc.json()
            if isinstance(cats_raw, dict):
                cats_raw = (cats_raw.get("data") or
                            cats_raw.get("categories") or [])
            if isinstance(cats_raw, list):
                for cat in sorted(cats_raw, key=lambda c: c.get("seq") or 0):
                    cname = (cat.get("name") or cat.get("categoryName") or "").strip()
                    raw_w = cat.get("weight") if cat.get("weight") is not None \
                            else cat.get("categoryWeight")
                    cid   = cat.get("categoryID") or cat.get("id")
                    if cname:
                        sup_cats[cname]          = _num(raw_w)
                        sup_lower[cname.lower()] = cname
                        if cid is not None:
                            cat_by_id[cid]          = cname
                            cat_by_id[str(cid)]     = cname
    except Exception:
        pass

    # ── Path 1 + 2 + 3: detail endpoints (categories-first, assignments nested) ──
    detail_endpoints = [
        # Dublin USD specific: uses scoreID from the grading task, not sectionID.
        # Response shape: { details: [{ categories: [{ groupID, name, weight,
        #   assignments: [{ objectSectionID, assignmentName, totalPoints,
        #                   score, scorePercentage, dueDate, missing, late }] }] }] }
        *(
            [f"{BASE}/campus/resources/portal/grades/detail/{score_id}"
             f"?appName={app_name}"]
            if score_id else []
        ),
        # Modern prism API
        f"{BASE}/campus/prism/api/portal/grades/assignmentDetail"
        f"?courseSectionID={sec_id}&appName={app_name}",
        # Legacy resources API
        f"{BASE}/campus/resources/portal/assignment"
        f"?courseSectionID={sec_id}&appName={app_name}",
        # Fallback with sectionID (what we tried before)
        f"{BASE}/campus/resources/portal/grades/detail/{sec_id}"
        f"?appName={app_name}",
    ]

    # Compute semester window now so detail results can be filtered by date
    sem_start, sem_end, _ = _resolve_semester_range(ic_terms)

    for url in detail_endpoints:
        try:
            r = s.get(url, timeout=20)
            if r.status_code != 200:
                continue
            body = r.json()
            result = _parse_detail_response(body, marks_by_id)
            if result is not None:
                assignments, categories = result
                # Filter to current semester only — the detail endpoint returns
                # the full year; we only want assignments whose dueDate falls
                # within the resolved semester window.
                assignments = [
                    a for a in assignments
                    if not a["date"] or sem_start <= a["date"] <= sem_end
                ]
                # Merge supplemental categories so empty ones (e.g. Finals)
                # still appear even if the detail endpoint omitted them.
                seen_names_lower = {c["name"].lower() for c in categories}
                for cname, cweight in sup_cats.items():
                    if cname.lower() not in seen_names_lower:
                        categories.append({"name": cname, "weight": cweight})
                return assignments, categories
        except Exception:
            continue

    # ── Path 4 (byDateRange fallback) ─────────────────────────────────────────
    # byDateRange returns assignments with NO categoryID/categoryName — only a
    # groupActivityID that links to the section-level assignment record.
    # We build a groupActivityID→category map by fetching the section's group
    # activities from the teacher-side API (students can read it too).

    def _make_categories():
        return [{"name": k, "weight": v} for k, v in sup_cats.items()]

    # Build groupActivityID → canonical category name
    ga_to_cat: dict = {}
    ga_endpoints = [
        f"{BASE}/campus/api/campus/grading/groupActivity?sectionID={sec_id}",
        f"{BASE}/campus/resources/section/teacherSections/groupActivity?sectionID={sec_id}",
        f"{BASE}/campus/resources/section/teacherSections/assignment?sectionID={sec_id}",
        f"{BASE}/campus/api/campus/grading/assignment?sectionID={sec_id}",
        # With _calendarID param (teacherSections pattern uses underscores)
        *(
            [
                f"{BASE}/campus/resources/section/teacherSections/groupActivity"
                f"?_calendarID={calendar_id}&_sectionID={sec_id}",
                f"{BASE}/campus/resources/section/teacherSections/assignment"
                f"?_calendarID={calendar_id}&_sectionID={sec_id}",
            ]
            if calendar_id else []
        ),
    ]
    for url in ga_endpoints:
        try:
            rg = s.get(url, timeout=15)
            if rg.status_code != 200:
                continue
            body = rg.json()
            if isinstance(body, dict):
                body = (body.get("data") or body.get("assignments") or
                        body.get("groupActivities") or [])
            if not isinstance(body, list) or not body:
                continue
            for item in body:
                if not isinstance(item, dict):
                    continue
                ga_id = item.get("groupActivityID") or item.get("activityID")
                cid   = item.get("categoryID") or item.get("groupID")
                if ga_id is not None and cid is not None:
                    cat = cat_by_id.get(cid) or cat_by_id.get(str(cid))
                    if cat:
                        ga_to_cat[ga_id]      = cat
                        ga_to_cat[str(ga_id)] = cat
            if ga_to_cat:
                break   # found a working endpoint
        except Exception:
            continue

    # sem_start / sem_end already computed above
    try:
        r = s.get(
            f"{BASE}/campus/api/portal/assignment/byDateRange",
            params={
                "startDate": sem_start + "T00:00:00",
                "endDate":   sem_end   + "T00:00:00",
                "sectionID": sec_id,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return [], _make_categories()
        raw = r.json()
    except Exception:
        return [], _make_categories()

    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("assignments") or []
    if not isinstance(raw, list):
        return [], _make_categories()

    assignments = []
    extra_cats  = {}

    for a in raw:
        # byDateRange uses objectSectionID as the student-specific assignment ID;
        # groupActivityID is the section-level assignment ID used in the gradebook.
        aid   = a.get("assignmentID") or a.get("_id") or a.get("objectSectionID")
        ga_id = a.get("groupActivityID")
        name  = a.get("assignmentName") or a.get("name") or "Assignment"
        total = _num(a.get("totalPoints") or a.get("pointsPossible"))
        date  = a.get("dueDate") or a.get("assignedDate") or ""

        # ── Category resolution — try every possible source ───────────────────
        cat  = None
        mark = {}   # may be populated in step 4; initialised here so earned lookup below is safe

        # 1. groupActivityID → ga_to_cat map (best source for byDateRange)
        if ga_id is not None and ga_to_cat:
            cat = ga_to_cat.get(ga_id) or ga_to_cat.get(str(ga_id))

        # 2. Direct categoryID field on the assignment (future-proofing)
        if cat is None:
            cid = a.get("categoryID") or a.get("groupID")
            if cid is not None:
                cat = cat_by_id.get(cid) or cat_by_id.get(str(cid))

        # 3. Direct categoryName / groupName fields on the assignment
        if cat is None:
            cat_raw = (a.get("categoryName") or a.get("groupName") or
                       a.get("category") or a.get("catName") or "").strip()
            if cat_raw:
                cat = sup_lower.get(cat_raw.lower(), cat_raw)

        # 4. Check the corresponding mark (if marks are available)
        if cat is None and marks_by_id:
            mark = (marks_by_id.get(aid) or marks_by_id.get(str(aid)) or
                    marks_by_id.get(ga_id) or marks_by_id.get(str(ga_id) if ga_id else "") or {})
            cid = mark.get("categoryID") or mark.get("groupID")
            if cid is not None:
                cat = cat_by_id.get(cid) or cat_by_id.get(str(cid))
            if cat is None:
                cat_raw = (mark.get("categoryName") or mark.get("groupName") or "").strip()
                if cat_raw:
                    cat = sup_lower.get(cat_raw.lower(), cat_raw)

        if cat is None:
            cat = "Uncategorized"

        _sw = sup_cats.get(cat)
        weight = _sw if _sw is not None else _num(a.get("categoryWeight") or a.get("weight"))

        if cat and cat not in sup_cats and cat not in extra_cats:
            extra_cats[cat] = weight

        for _k in ("scorePoints", "pointsEarned", "score"):
            _v = a.get(_k)
            if _v is not None:
                earned = _num(_v)
                if earned is not None:
                    break
        else:
            earned = None
        if earned is None and mark:
            for _k in ("score", "scorePoints", "pointsEarned", "earnedPoints"):
                _v = mark.get(_k)
                if _v is not None:
                    earned = _num(_v)
                    if earned is not None:
                        break

        assignments.append({
            "id":           aid,
            "name":         name,
            "earned":       earned,
            "total":        total,
            "category":     cat,
            "weight":       weight,
            "date":         str(date)[:10],
            "hypothetical": False,
        })

    assignments.sort(key=lambda x: x["date"], reverse=True)
    all_cats = _make_categories()
    for cname, cweight in extra_cats.items():
        all_cats.append({"name": cname, "weight": cweight})
    return assignments, all_cats


# ── GPA calculation ───────────────────────────────────────────────────────────

def _detect_course_type(name):
    u = name.upper()
    if "AP " in u or "ADVANCED PLACEMENT" in u:
        return "AP"
    if "HONORS" in u or "HON " in u or "(H)" in u or "(HP)" in u:
        return "HONORS"
    return "STANDARD"


def _pct_to_letter(pct):
    if pct >= 97: return "A+"
    if pct >= 93: return "A"
    if pct >= 90: return "A-"
    if pct >= 87: return "B+"
    if pct >= 83: return "B"
    if pct >= 80: return "B-"
    if pct >= 77: return "C+"
    if pct >= 73: return "C"
    if pct >= 70: return "C-"
    if pct >= 67: return "D+"
    if pct >= 63: return "D"
    if pct >= 60: return "D-"
    return "F"


def _letter_to_gpa(letter, course_type="STANDARD"):
    base = {
        "A+": 4.0, "A": 4.0, "A-": 3.7,
        "B+": 3.3, "B": 3.0, "B-": 2.7,
        "C+": 2.3, "C": 2.0, "C-": 1.7,
        "D+": 1.3, "D": 1.0, "D-": 0.7,
        "F":  0.0,
    }
    pts   = base.get(letter, 0.0)
    boost = (1.0 if course_type in ("AP", "DUAL_ENROLLMENT")
             else 0.5 if course_type == "HONORS"
             else 0.0)
    return min(pts + boost, 5.0) if pts > 0 else 0.0


def compute_gpa(courses):
    """
    Compute weighted and unweighted GPA across all courses that have a grade.
    Each course is treated as 1 credit (equal weight).
    Weighted GPA adds +1.0 for AP, +0.5 for Honors, per Vela / Dublin USD convention.
    """
    breakdown   = []
    w_total     = 0.0
    u_total     = 0.0
    count       = 0

    for c in courses:
        pct = c.get("percent")
        if pct is None:
            continue
        ctype  = _detect_course_type(c.get("name", ""))
        letter = _pct_to_letter(pct)
        w_pts  = _letter_to_gpa(letter, ctype)
        u_pts  = _letter_to_gpa(letter, "STANDARD")
        w_total += w_pts
        u_total += u_pts
        count   += 1
        breakdown.append({
            "name":       c["name"],
            "pct":        pct,
            "letter":     letter,
            "type":       ctype,
            "weighted":   w_pts,
            "unweighted": u_pts,
        })

    if count == 0:
        return {"weighted": None, "unweighted": None, "breakdown": []}

    return {
        "weighted":   round(w_total / count, 2),
        "unweighted": round(u_total / count, 2),
        "breakdown":  breakdown,
    }


# ── grade simulation math ─────────────────────────────────────────────────────

def compute_percent(course, extra_assignment=None):
    """
    Compute overall % for a course from individual assignments.

    Algorithm (mirrors IC's weighted-category model):
      1. Group graded assignments by category (case-insensitive).
      2. Per category: cat_avg = sum(earned) / sum(total).
      3. If any category has a known weight:
           overall = Σ(cat_avg × weight) / Σ(weight)
         where only categories that *have graded assignments* contribute
         their weight to the denominator.
      4. Otherwise: simple points total across all assignments.
      5. If no assignments at all: fall back to IC's reported points/percent.

    extra_assignment: {earned, total, category, weight} to add hypothetically.
    """
    items = list(course.get("assignments", []))
    if extra_assignment:
        items.append(extra_assignment)

    if not items:
        # Fall back to IC's own reported numbers
        earned = course.get("earned")
        total  = course.get("total")
        if earned is not None and total:
            return round((earned / total) * 100, 2)
        return course.get("percent")

    # Build case-insensitive weight map from categories list
    categories = course.get("categories", [])
    cat_weights = {}                          # lower(name) -> weight
    for c in categories:
        if c.get("weight") is not None:
            cat_weights[c["name"].strip().lower()] = c["weight"]

    # Accumulate per-category earned/total, keyed by normalised name
    cat_totals = {}   # lower(name) -> {"e": float, "t": float, "w": float|None}
    for a in items:
        if a.get("earned") is None or not a.get("total"):
            continue                           # skip ungraded / zero-point rows
        raw_cat  = (a.get("category") or "Uncategorized").strip()
        norm_cat = raw_cat.lower()
        if norm_cat not in cat_totals:
            # Prefer category-list weight; fall back to assignment's own weight field
            w = cat_weights.get(norm_cat, a.get("weight"))
            cat_totals[norm_cat] = {"e": 0.0, "t": 0.0, "w": w}
        cat_totals[norm_cat]["e"] += float(a["earned"])
        cat_totals[norm_cat]["t"] += float(a["total"])

    if not cat_totals:
        earned = course.get("earned")
        total  = course.get("total")
        if earned is not None and total:
            return round((earned / total) * 100, 2)
        return course.get("percent")

    has_weights = any(v["w"] is not None for v in cat_totals.values())
    if has_weights:
        wsum = 0.0
        wtot = 0.0
        for v in cat_totals.values():
            if v["w"] is not None and v["t"] > 0:
                wsum += (v["e"] / v["t"]) * v["w"]
                wtot += v["w"]
        if wtot > 0:
            return round((wsum / wtot) * 100, 2)

    # Fallback: simple points-total across all categories
    e = sum(v["e"] for v in cat_totals.values())
    t = sum(v["t"] for v in cat_totals.values())
    return round((e / t) * 100, 2) if t else None


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard") if _state() else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error       = None
    base_url    = ""
    app_name    = ""
    district_name = ""
    if request.method == "POST":
        u            = request.form.get("username", "").strip()
        p            = request.form.get("password", "")
        base_url     = request.form.get("base_url", "").strip()
        app_name     = request.form.get("app_name", "").strip()
        district_name = request.form.get("district_name", "").strip()
        if not u or not p:
            error = "Enter a username and password."
        elif not base_url:
            error = "Select your school or district."
        else:
            s, resolved_base, resolved_app, err = ic_login(u, p, base_url, app_name)
            if err:
                error = err
            else:
                STORE[_sid()] = {
                    "session":  s,
                    "courses":  None,
                    "base_url": resolved_base,
                    "app_name": resolved_app,
                }
                return redirect(url_for("dashboard"))
    return render_template("login.html", error=error,
                           base_url=base_url, app_name=app_name,
                           district_name=district_name)


@app.route("/logout")
def logout():
    STORE.pop(_sid(), None)
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    st = _state()
    if not st:
        return redirect(url_for("login"))
    if st["courses"] is None:
        courses, err, raw = fetch_grades(
            st["session"],
            base_url=st.get("base_url"),
            app_name=st.get("app_name"),
        )
        if err:
            return render_template("error.html", error=err, raw=raw), 500
        st["courses"] = courses
    gpa = compute_gpa(st["courses"])
    return render_template("dashboard.html", courses=st["courses"], gpa=gpa)


@app.route("/refresh")
def refresh():
    st = _state()
    if not st:
        return redirect(url_for("login"))
    st["courses"] = None
    return redirect(url_for("dashboard"))


@app.route("/course/<int:idx>")
def course(idx):
    st = _state()
    if not st or not st["courses"]:
        return redirect(url_for("login"))
    if idx < 0 or idx >= len(st["courses"]):
        return redirect(url_for("dashboard"))
    c = st["courses"][idx]
    current = compute_percent(c)
    return render_template("course.html", course=c, idx=idx, current=current)


@app.route("/course/<int:idx>/simulate", methods=["POST"])
def simulate(idx):
    """
    Recalculate grade from a client-supplied assignment list.
    Body: { assignments: [{earned, total, category, weight}, ...],
            categories:  [{name, weight}, ...] }
    """
    st = _state()
    if not st or not st["courses"]:
        return jsonify({"error": "not logged in"}), 401
    c    = st["courses"][idx]
    data = request.get_json(force=True)

    # Build a synthetic course-like dict from the client's current state
    client_assignments = data.get("assignments") or []
    client_categories  = data.get("categories")  or c.get("categories") or []

    synthetic = {
        "assignments": client_assignments,
        "categories":  client_categories,
        "earned":      c.get("earned"),
        "total":       c.get("total"),
        "percent":     c.get("percent"),
    }
    projected = compute_percent(synthetic)
    return jsonify({"projected": projected})


@app.route("/course/<int:idx>/final", methods=["POST"])
def final_calc(idx):
    """
    Body: { current: float|null, desired: float, weight: float,
            assignments: [...], categories: [...] }
    If current is null, compute it from assignments/categories.
    """
    st = _state()
    if not st or not st["courses"]:
        return jsonify({"error": "not logged in"}), 401
    c    = st["courses"][idx]
    data = request.get_json(force=True)

    desired = _num(data.get("desired"))
    weight  = _num(data.get("weight"))
    if desired is None or not weight or weight <= 0 or weight >= 100:
        return jsonify({"error": "Enter a valid desired grade and final weight (1–99)."}), 400

    # Use client-supplied current if provided, else compute from their assignment list
    current = _num(data.get("current"))
    if current is None:
        client_assignments = data.get("assignments") or []
        client_categories  = data.get("categories")  or c.get("categories") or []
        synthetic = {
            "assignments": client_assignments,
            "categories":  client_categories,
            "earned":      c.get("earned"),
            "total":       c.get("total"),
            "percent":     c.get("percent"),
        }
        current = compute_percent(synthetic)
    if current is None:
        return jsonify({"error": "No current grade available to calculate from."}), 400

    w      = weight / 100.0
    needed = (desired - current * (1 - w)) / w
    return jsonify({"current": round(current, 2), "needed": round(needed, 2)})


@app.route("/debug")
def debug():  # noqa – keep for field-name verification
    """Show raw responses from the two real assignment endpoints."""
    st = _state()
    if not st:
        return redirect(url_for("login"))
    s = st["session"]

    if st["courses"] is None:
        courses, _, _ = fetch_grades(st["session"])
        st["courses"] = courses or []

    results = {}
    courses = st["courses"] or []
    if not courses:
        return app.response_class('{"error": "no courses loaded"}', mimetype="application/json")

    # Grab personID + calendarID from grades data
    raw_data  = s.get(GRADES_API, timeout=20).json()
    person_id = None
    cal_id    = raw_data[0].get("calendarID") if raw_data else None
    for term in (raw_data[0].get("terms", []) if raw_data else []):
        for course in term.get("courses", []):
            for task in course.get("gradingTasks", []):
                if task.get("personID"):
                    person_id = task["personID"]
                    break
            if person_id:
                break
        if person_id:
            break

    # 1. assignmentMark — all scores for this student
    try:
        rm = s.get(
            f"{BASE}/campus/resources/section/teacherSections/assignmentMark",
            params={"_calendarID": cal_id, "_personID": person_id},
            timeout=20,
        )
        body = rm.json() if rm.status_code == 200 else rm.text[:500]
        # Truncate big lists — show first 3 items
        if isinstance(body, list) and len(body) > 3:
            body = body[:3]
        results["assignmentMark"] = {"status": rm.status_code, "personID": person_id,
                                     "calendarID": cal_id, "sample": body}
    except Exception as e:
        results["assignmentMark"] = {"error": str(e)}

    # 2. byDateRange — for first course only
    c = courses[0]
    try:
        ra = s.get(
            f"{BASE}/campus/api/portal/assignment/byDateRange",
            params={
                "startDate": "2025-08-01T00:00:00",
                "endDate":   "2026-06-30T00:00:00",
                "sectionID": c["section_id"],
            },
            timeout=20,
        )
        body = ra.json() if ra.status_code == 200 else ra.text[:500]
        if isinstance(body, list) and len(body) > 3:
            body = body[:3]
        results["byDateRange"] = {"status": ra.status_code, "course": c["name"], "sample": body}
    except Exception as e:
        results["byDateRange"] = {"error": str(e)}

    return app.response_class(_json.dumps(results, indent=2), mimetype="application/json")


@app.route("/debug/cats")
def debug_cats():
    """Try every plausible category/grade-detail endpoint and show what works."""
    st = _state()
    if not st:
        return redirect(url_for("login"))
    if st["courses"] is None:
        return app.response_class('{"error": "not loaded — visit /dashboard first"}',
                                  mimetype="application/json")

    s = st["session"]
    c = (st["courses"] or [None])[0]
    if not c:
        return app.response_class('{"error": "no courses"}', mimetype="application/json")

    sec_id   = c["section_id"]
    score_id = c.get("score_id")
    task_id  = 3   # observed in assignment payload

    candidates = [
        # gradebook-style endpoints
        f"{BASE}/campus/api/portal/gradebook?sectionID={sec_id}",
        f"{BASE}/campus/resources/portal/gradebook?sectionID={sec_id}",
        f"{BASE}/campus/api/portal/studentGradebook?sectionID={sec_id}",
        f"{BASE}/campus/resources/portal/studentGradebook?sectionID={sec_id}",
        f"{BASE}/campus/api/portal/portalGradebook?sectionID={sec_id}",
        # categories with taskID
        f"{BASE}/campus/resources/portal/categories?sectionID={sec_id}&taskID={task_id}",
        f"{BASE}/campus/api/portal/categories?sectionID={sec_id}&taskID={task_id}",
        f"{BASE}/campus/api/portal/category?sectionID={sec_id}&taskID={task_id}",
        # scoreID based
        f"{BASE}/campus/api/portal/grades/score/{score_id}" if score_id else None,
        f"{BASE}/campus/resources/portal/grades/score/{score_id}" if score_id else None,
        f"{BASE}/campus/api/portal/score/{score_id}" if score_id else None,
        # sectionID in URL path
        f"{BASE}/campus/api/portal/sections/{sec_id}/categories",
        f"{BASE}/campus/api/portal/sections/{sec_id}/grades",
        f"{BASE}/campus/api/portal/section/{sec_id}",
        # course info
        f"{BASE}/campus/api/portal/course?sectionID={sec_id}",
        f"{BASE}/campus/resources/portal/course?sectionID={sec_id}",
        f"{BASE}/campus/api/portal/sectionInfo?sectionID={sec_id}",
        # task-based
        f"{BASE}/campus/api/portal/grades/task?sectionID={sec_id}&taskID={task_id}",
        f"{BASE}/campus/resources/portal/gradingTask?sectionID={sec_id}&taskID={task_id}",
        # underscore variants
        f"{BASE}/campus/resources/portal/categories?_sectionID={sec_id}",
        # scoreList
        f"{BASE}/campus/api/portal/scoreList?sectionID={sec_id}",
        f"{BASE}/campus/resources/portal/scoreList?sectionID={sec_id}",
    ]
    results = {}
    for url in candidates:
        if not url:
            continue
        try:
            r = s.get(url, timeout=10)
            body = None
            if r.status_code == 200:
                try:
                    body = r.json()
                    if isinstance(body, list) and len(body) > 2:
                        body = body[:2]
                except Exception:
                    body = r.text[:500]
            elif r.status_code not in (404, 500):
                body = r.text[:300]
            results[url] = {"status": r.status_code, "sample": body}
        except Exception as e:
            results[url] = {"error": str(e)}

    return app.response_class(
        _json.dumps({"course": c["name"], "section_id": sec_id,
                     "score_id": score_id, "results": results}, indent=2),
        mimetype="application/json",
    )


@app.route("/debug/assignments")
def debug_assignments():
    """Show raw field names on assignments + marks so we can confirm category linking."""
    st = _state()
    if not st:
        return redirect(url_for("login"))
    if st["courses"] is None:
        return app.response_class('{"error":"visit /dashboard first"}',
                                  mimetype="application/json")
    s   = st["session"]
    c   = (st["courses"] or [None])[0]
    if not c:
        return app.response_class('{"error":"no courses"}', mimetype="application/json")

    sec_id = c["section_id"]
    out    = {"course": c["name"], "section_id": sec_id}

    # 1. Categories endpoint — show all fields on first cat
    try:
        rc = s.get(f"{BASE}/campus/api/campus/grading/categories",
                   params={"sectionID": sec_id}, timeout=15)
        cats_raw = rc.json() if rc.status_code == 200 else []
        if isinstance(cats_raw, dict):
            cats_raw = cats_raw.get("data") or cats_raw.get("categories") or []
        out["categories_status"] = rc.status_code
        out["categories_sample"] = cats_raw[:3] if isinstance(cats_raw, list) else cats_raw
    except Exception as e:
        out["categories_error"] = str(e)

    # 2. byDateRange — show ALL keys on first 3 assignments
    sem_start, sem_end, _ = _resolve_semester_range(None)  # debug: no IC terms
    try:
        ra = s.get(f"{BASE}/campus/api/portal/assignment/byDateRange",
                   params={"startDate": sem_start + "T00:00:00",
                           "endDate":   sem_end   + "T00:00:00",
                           "sectionID": sec_id}, timeout=20)
        raw = ra.json() if ra.status_code == 200 else []
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("assignments") or []
        out["byDateRange_status"] = ra.status_code
        out["byDateRange_sample"] = raw[:3] if isinstance(raw, list) else raw
    except Exception as e:
        out["byDateRange_error"] = str(e)

    # 3. prism assignmentDetail — show first 1 category with first 2 assignments
    try:
        rp = s.get(
            f"{BASE}/campus/prism/api/portal/grades/assignmentDetail",
            params={"courseSectionID": sec_id, "appName": "dublin"}, timeout=20)
        out["prism_status"] = rp.status_code
        if rp.status_code == 200:
            body = rp.json()
            data = body.get("data") or body if isinstance(body, list) else []
            if isinstance(data, list) and data:
                sample = dict(data[0])
                if "assignments" in sample and isinstance(sample["assignments"], list):
                    sample["assignments"] = sample["assignments"][:2]
                out["prism_sample"] = sample
        else:
            out["prism_body"] = rp.text[:300]
    except Exception as e:
        out["prism_error"] = str(e)

    # 4. resources/portal/assignment
    try:
        rr = s.get(f"{BASE}/campus/resources/portal/assignment",
                   params={"courseSectionID": sec_id, "appName": "dublin"}, timeout=20)
        out["resources_assignment_status"] = rr.status_code
        if rr.status_code == 200:
            body = rr.json()
            out["resources_assignment_sample"] = (
                body[:2] if isinstance(body, list) else
                {k: v[:2] if isinstance(v, list) else v
                 for k, v in (body.items() if isinstance(body, dict) else {}.items())}
            )
        else:
            out["resources_assignment_body"] = rr.text[:300]
    except Exception as e:
        out["resources_assignment_error"] = str(e)

    # 5. groupActivity endpoints — these should link groupActivityID → categoryID
    ga_candidates = [
        f"{BASE}/campus/api/campus/grading/groupActivity?sectionID={sec_id}",
        f"{BASE}/campus/resources/section/teacherSections/groupActivity?sectionID={sec_id}",
        f"{BASE}/campus/resources/section/teacherSections/assignment?sectionID={sec_id}",
        f"{BASE}/campus/api/campus/grading/assignment?sectionID={sec_id}",
    ]
    out["groupActivity_probes"] = {}
    for url in ga_candidates:
        try:
            r = s.get(url, timeout=10)
            body = None
            if r.status_code == 200:
                try:
                    body = r.json()
                    if isinstance(body, list) and len(body) > 2:
                        body = {"list_len": len(body), "sample": body[:2]}
                except Exception:
                    body = r.text[:400]
            elif r.status_code != 404:
                body = r.text[:200]
            out["groupActivity_probes"][url] = {"status": r.status_code, "body": body}
        except Exception as e:
            out["groupActivity_probes"][url] = {"error": str(e)}

    # Show the groupActivityIDs from the byDateRange sample so we can cross-check
    bdr = out.get("byDateRange_sample", [])
    out["byDateRange_groupActivityIDs"] = [
        {"name": a.get("assignmentName"), "groupActivityID": a.get("groupActivityID"),
         "objectSectionID": a.get("objectSectionID")}
        for a in (bdr if isinstance(bdr, list) else [])
    ]

    return app.response_class(
        _json.dumps(out, indent=2, default=str), mimetype="application/json"
    )


@app.route("/debug/grading")
def debug_grading():
    """Probe /campus/api/campus/grading/* family for an assignments endpoint
    that returns categoryID embedded. This is the same path family as the
    working categories endpoint, so it's the most likely place."""
    st = _state()
    if not st:
        return redirect(url_for("login"))
    if st["courses"] is None:
        return app.response_class('{"error": "visit /dashboard first"}',
                                  mimetype="application/json")
    s = st["session"]
    c = (st["courses"] or [None])[0]
    if not c:
        return app.response_class('{"error": "no courses"}', mimetype="application/json")

    sec_id = c["section_id"]

    # Try every combination of: a handful of root paths × resource names × param styles
    roots = [
        f"{BASE}/campus/api/campus/grading",
        f"{BASE}/campus/api/portal/grading",
        f"{BASE}/campus/api/portal",
        f"{BASE}/campus/resources/portal",
        f"{BASE}/campus/api/student/grading",
        f"{BASE}/campus/api/grading",
    ]
    resources = [
        "assignment", "assignments",
        "assignmentMark", "assignmentMark2",
        "scoreList", "score", "scores",
        "studentScore", "studentAssignment", "studentAssignments",
        "gradebook", "sectionGradebook", "portalGradebook",
        "curriculumGradingTask", "gradingTask",
        "sectionAssignment", "sectionAssignments",
        "pickOne", "gradeDetail",
    ]
    param_styles = [
        lambda r, p: f"{r}/{p}?sectionID={sec_id}",
        lambda r, p: f"{r}/{p}?_sectionID={sec_id}",
        lambda r, p: f"{r}/{p}?_section={sec_id}",
    ]
    candidates = []
    for root in roots:
        for res in resources:
            for fmt in param_styles:
                candidates.append(fmt(root, res))
    # Also try section-in-path variants
    for root in roots:
        candidates.append(f"{root}/section/{sec_id}/assignment")
        candidates.append(f"{root}/section/{sec_id}/assignments")
        candidates.append(f"{root}/{sec_id}/assignment")
    results = {}
    counts = {"200": 0, "404": 0, "other": 0}
    for url in candidates:
        try:
            r = s.get(url, timeout=10)
            code = r.status_code
            if code == 404:
                counts["404"] += 1
                continue  # skip 404s from the output entirely
            if code == 200:
                counts["200"] += 1
                try:
                    body = r.json()
                    if isinstance(body, list) and len(body) > 2:
                        body = {"__list_len__": len(body), "sample": body[:2]}
                except Exception:
                    body = r.text[:400]
                results[url] = {"status": code, "body": body}
            else:
                counts["other"] += 1
                results[url] = {"status": code, "body": r.text[:200]}
        except Exception as e:
            counts["other"] += 1
            results[url] = {"error": str(e)}
    return app.response_class(
        _json.dumps({"course": c["name"], "section_id": sec_id,
                     "tried": len(candidates), "counts": counts,
                     "results": results}, indent=2, default=str),
        mimetype="application/json",
    )


@app.route("/debug/raw")
def debug_raw():
    """Dump the FULL /campus/resources/portal/grades response for ONE course.
    Categories might be nested inside gradingTasks — need to see everything."""
    st = _state()
    if not st:
        return redirect(url_for("login"))
    s = st["session"]

    try:
        r = s.get(GRADES_API, timeout=20)
        data = r.json()
    except Exception as e:
        return app.response_class(f'{{"error": "{e}"}}', mimetype="application/json")

    # Trim to first course of first term that has a grade
    out = {"status": r.status_code, "keys_at_top": list(data[0].keys()) if data else []}
    if data and isinstance(data, list):
        enrollment = data[0]
        terms = enrollment.get("terms", [])
        if terms:
            # Get the first term's first course (full raw structure)
            first_term = terms[0]
            out["term_keys"]       = list(first_term.keys())
            out["term_name"]       = first_term.get("termName")
            out["term_startDate"]  = first_term.get("startDate")
            out["term_endDate"]    = first_term.get("endDate")
            courses_list = first_term.get("courses", [])
            if courses_list:
                first_course = courses_list[0]
                out["course_keys"]    = list(first_course.keys())
                out["sample_course"]  = first_course   # full raw course object
    return app.response_class(_json.dumps(out, indent=2, default=str), mimetype="application/json")


@app.route("/api/districts")
def api_districts():
    """Proxy IC's district search so the browser doesn't CORS-block it."""
    q     = request.args.get("q", "").strip()
    state = request.args.get("state", "CA").strip()
    if len(q) < 3:
        return jsonify({"data": []})
    try:
        r = requests.get(
            IC_DISTRICT_SEARCH,
            params={"query": q, "state": state},
            headers={"User-Agent": "Mozilla/5.0 (GradeCalc)"},
            timeout=10,
        )
        return jsonify(r.json() if r.status_code == 200 else {"data": []})
    except Exception:
        return jsonify({"data": []})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=port, debug=debug)
