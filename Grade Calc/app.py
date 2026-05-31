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


#  helpers

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
    from datetime import date as _date

    today = datetime.now().date()

    def _parse(s):
        if not s:
            return None
        try:
            return _date.fromisoformat(str(s)[:10])
        except Exception:
            return None

    sorted_terms = sorted(
        [t for t in (ic_terms or []) if isinstance(t, dict)],
        key=lambda t: t.get("seq") or t.get("termSeq") or 0,
    )
    if not sorted_terms:
        return _current_semester_range()

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

    if f_start and f_end and f_start <= today <= f_end:
        return str(f_start), str(f_end), "Semester 1"
    if s_start and s_end and s_start <= today <= s_end:
        return str(s_start), str(s_end), "Semester 2"

    if f_start and s_start:
        f_dist = abs((today - f_end).days) if f_end else 99999
        s_dist = abs((today - s_end).days) if s_end else 99999
        if f_dist <= s_dist:
            return str(f_start), str(f_end), "Semester 1"
        else:
            return str(s_start), str(s_end), "Semester 2"

    if f_start:
        return str(f_start), str(f_end), "Semester 1"
    if s_start:
        return str(s_start), str(s_end), "Semester 2"

    return _current_semester_range()


# auth

def ic_login(username, password, base_url, app_name):
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
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


#  grade fetching

def _best_task(grading_tasks):
    for want in ("Semester Grade", "Term Grade", "Progress Grade"):
        for t in grading_tasks:
            if t.get("taskName", "") == want:
                if t.get("progressPercent") is not None or t.get("percent") is not None:
                    return t
    for t in grading_tasks:
        if t.get("progressPercent") is not None or t.get("percent") is not None:
            return t
    return None


def fetch_grades(s: requests.Session, base_url=None, app_name=None):
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

    enrollment = data[0]

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
                    for m in marks_list.get("data", []):
                        aid = m.get("assignmentID") or m.get("_id")
                        if aid is not None:
                            marks_by_id[aid] = m
        except Exception:
            pass

    terms_raw = sorted(
        enrollment.get("terms", []),
        key=lambda t: t.get("termSeq", 0),
        reverse=True,
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
                continue

            task = _best_task(course.get("gradingTasks", []))
            if task is None:
                continue

            seen.add(sec_id)
            pct    = _num(task.get("progressPercent")) or _num(task.get("percent"))
            letter = task.get("progressScore") or task.get("score") or ""
            earned = _num(task.get("progressPointsEarned"))
            total  = _num(task.get("progressTotalPoints"))
            score_id    = task.get("scoreID")
            has_detail  = task.get("hasDetail", False)

            courses.append({
                "section_id":    sec_id,
                "name":          course.get("courseName", "Unknown Course"),
                "teacher":       course.get("teacherDisplay", ""),
                "section":       str(course.get("sectionNumber", "")),
                "current_term":  term_name,
                "term_start":    term_start,
                "term_end":      term_end,
                "percent":       pct,
                "letter":        letter,
                "earned":        earned,
                "total":         total,
                "score_id":      score_id,
                "has_detail":    has_detail,
                "assignments":   [],
                "categories":    [],
                "group_weighted": False,   # filled after _fetch_assignments
                "term_history":  [],
            })

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

    all_ic_terms = []
    for term in terms_raw:
        all_ic_terms.append({
            "seq":       term.get("termSeq", 0),
            "startDate": term.get("startDate"),
            "endDate":   term.get("endDate"),
            "termName":  term.get("termName", ""),
        })

    # CHANGE 3: unpack group_weighted from _fetch_assignments
    for c in courses:
        assignments, categories, group_weighted = _fetch_assignments(
            s, c, marks_by_id,
            calendar_id=calendar_id,
            person_id=person_id,
            ic_terms=all_ic_terms,
            base_url=base_url,
            app_name=app_name,
        )
        c["assignments"]    = assignments
        c["categories"]     = categories
        c["group_weighted"] = group_weighted

    return courses, None, None


def _parse_detail_response(body, marks_by_id):
    """
    Parse IC's assignment detail response into (assignments, categories, group_weighted).

    Handles three shapes:

    Shape A — Dublin USD (resources/portal/grades/detail/{scoreID}):
      { details: [{ task: {groupWeighted}, categories: [{ groupID, name, weight,
          assignments: [...] }] }] }

    Shape B — Prism / modern IC:
      { data: [{ categoryID, categoryName, weight, assignments: [...] }] }

    Shape C — Legacy Task[]:
      { Task: [{ taskName, weight, Assignments: [...] }] }

    Returns (assignments, categories, group_weighted) or None if not recognised.
    """
    # CHANGE 1: Shape A — extract groupWeighted from task
    if isinstance(body, dict) and body.get("details"):
        details = body["details"]
        if not isinstance(details, list):
            return None
        cat_order  = []
        cat_map    = {}
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
                    aid   = (a.get("objectSectionID") or a.get("assignmentID") or
                             a.get("id") or a.get("_id"))
                    aname = a.get("assignmentName") or a.get("name") or "Assignment"
                    total = _num(a.get("totalPoints") or a.get("pointsPossible"))
                    date  = a.get("dueDate") or a.get("assignedDate") or ""
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

        # CHANGE 1: extract groupWeighted from first detail task
        group_weighted = False
        for detail in details:
            if isinstance(detail, dict) and detail.get("task"):
                gw = detail["task"].get("groupWeighted")
                if gw is not None:
                    group_weighted = bool(gw)
                    break

        return assignments, categories, group_weighted

    # Shape B / C
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
    # Shape B/C: assume weighted if weights are present
    group_weighted = any(c.get("weight") is not None for c in categories)
    return assignments, categories, group_weighted


def _fetch_assignments(s, course, marks_by_id=None, calendar_id=None, person_id=None, ic_terms=None, base_url=None, app_name=None):
    sec_id   = course["section_id"]
    score_id = course.get("score_id")
    _raw = (base_url or "https://icampus.dublinusd.org/campus").rstrip("/")
    BASE = _raw[:-7] if _raw.endswith("/campus") else _raw
    app_name = app_name or "dublin"

    sup_cats  = {}
    sup_lower = {}
    cat_by_id = {}
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

    detail_endpoints = [
        *(
            [f"{BASE}/campus/resources/portal/grades/detail/{score_id}"
             f"?appName={app_name}"]
            if score_id else []
        ),
        f"{BASE}/campus/prism/api/portal/grades/assignmentDetail"
        f"?courseSectionID={sec_id}&appName={app_name}",
        f"{BASE}/campus/resources/portal/assignment"
        f"?courseSectionID={sec_id}&appName={app_name}",
        f"{BASE}/campus/resources/portal/grades/detail/{sec_id}"
        f"?appName={app_name}",
    ]

    sem_start, sem_end, _ = _resolve_semester_range(ic_terms)

    for url in detail_endpoints:
        try:
            r = s.get(url, timeout=20)
            if r.status_code != 200:
                continue
            body = r.json()
            result = _parse_detail_response(body, marks_by_id)
            if result is not None:
                # CHANGE 2: unpack group_weighted
                assignments, categories, group_weighted = result
                assignments = [
                    a for a in assignments
                    if not a["date"] or sem_start <= a["date"] <= sem_end
                ]
                seen_names_lower = {c["name"].lower() for c in categories}
                for cname, cweight in sup_cats.items():
                    if cname.lower() not in seen_names_lower:
                        categories.append({"name": cname, "weight": cweight})
                # CHANGE 2: return group_weighted
                return assignments, categories, group_weighted
        except Exception:
            continue

    def _make_categories():
        return [{"name": k, "weight": v} for k, v in sup_cats.items()]

    ga_to_cat: dict = {}
    ga_endpoints = [
        f"{BASE}/campus/api/campus/grading/groupActivity?sectionID={sec_id}",
        f"{BASE}/campus/resources/section/teacherSections/groupActivity?sectionID={sec_id}",
        f"{BASE}/campus/resources/section/teacherSections/assignment?sectionID={sec_id}",
        f"{BASE}/campus/api/campus/grading/assignment?sectionID={sec_id}",
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
                break
        except Exception:
            continue

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
            # CHANGE 2: fallback returns False for group_weighted
            return [], _make_categories(), False
        raw = r.json()
    except Exception:
        return [], _make_categories(), False

    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("assignments") or []
    if not isinstance(raw, list):
        return [], _make_categories(), False

    assignments = []
    extra_cats  = {}

    for a in raw:
        aid   = a.get("assignmentID") or a.get("_id") or a.get("objectSectionID")
        ga_id = a.get("groupActivityID")
        name  = a.get("assignmentName") or a.get("name") or "Assignment"
        total = _num(a.get("totalPoints") or a.get("pointsPossible"))
        date  = a.get("dueDate") or a.get("assignedDate") or ""

        cat  = None
        mark = {}

        if ga_id is not None and ga_to_cat:
            cat = ga_to_cat.get(ga_id) or ga_to_cat.get(str(ga_id))

        if cat is None:
            cid = a.get("categoryID") or a.get("groupID")
            if cid is not None:
                cat = cat_by_id.get(cid) or cat_by_id.get(str(cid))

        if cat is None:
            cat_raw = (a.get("categoryName") or a.get("groupName") or
                       a.get("category") or a.get("catName") or "").strip()
            if cat_raw:
                cat = sup_lower.get(cat_raw.lower(), cat_raw)

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
    # byDateRange fallback: assume not group_weighted (total points)
    return assignments, all_cats, False


#  GPA calculation

def _detect_course_type(name):
    u = name.upper()
    if "AP " in u or "ADVANCED PLACEMENT" in u:
        return "AP"
    if "HONORS" in u or "HON " in u or "(H)" in u or "(HP)" in u:
        return "HONORS"
    return "STANDARD"


def _pct_to_letter(pct):
    if pct >= 96.5: return "A+"
    if pct >= 92.5: return "A"
    if pct >= 89.5: return "A-"
    if pct >= 86.5: return "B+"
    if pct >= 82.5: return "B"
    if pct >= 79.5: return "B-"
    if pct >= 76.5: return "C+"
    if pct >= 72.5: return "C"
    if pct >= 69.5: return "C-"
    if pct >= 66.5: return "D+"
    if pct >= 62.6: return "D"
    if pct >= 59.5: return "D-"
    return "F"


def _letter_to_gpa(letter, course_type="STANDARD"):
    base = {
        "A+": 4.0, "A": 4.0, "A-": 4.0,
        "B+": 3.0, "B": 3.0, "B-": 3.0,
        "C+": 2.0, "C": 2.0, "C-": 2.0,
        "D+": 1.0, "D": 1.0, "D-": 1.0,
        "F":  0.0,
    }
    pts   = base.get(letter, 0.0)
    boost = (1.0 if course_type in ("AP", "DUAL_ENROLLMENT")
             else 1.0 if course_type == "HONORS"
             else 0.0)
    return min(pts + boost, 5.0) if pts > 0 else 0.0


def compute_gpa(courses):
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


# CHANGE 4: compute_percent uses group_weighted flag per course
def compute_percent(course, extra_assignment=None):
    """
    Compute overall % for a course from individual assignments.

    Uses IC's groupWeighted flag per course:
      - groupWeighted=False: total points (earned / possible across all assignments)
      - groupWeighted=True:  weighted category averages
    Falls back to IC's reported earned/total if no assignments available.
    """
    items = list(course.get("assignments", []))
    if extra_assignment:
        items.append(extra_assignment)

    ic_earned      = course.get("earned")
    ic_total       = course.get("total")
    group_weighted = course.get("group_weighted", False)

    if not items:
        if ic_earned is not None and ic_total:
            return round((ic_earned / ic_total) * 100, 2)
        return course.get("percent")

    categories  = course.get("categories", [])
    cat_weights = {c["name"].strip().lower(): c["weight"]
                   for c in categories if c.get("weight") is not None}

    graded = [a for a in items if a.get("earned") is not None and a.get("total")]

    if not graded:
        if ic_earned is not None and ic_total:
            return round((ic_earned / ic_total) * 100, 2)
        return course.get("percent")

    if not group_weighted:
        # Total points mode — IC calcMethod:nu groupWeighted:false
        e = sum(float(a["earned"]) for a in graded)
        t = sum(float(a["total"])  for a in graded)
        return round((e / t) * 100, 2) if t else None

    # Weighted category mode — IC groupWeighted:true
    cat_totals = {}
    for a in graded:
        raw_cat  = (a.get("category") or "Uncategorized").strip()
        norm_cat = raw_cat.lower()
        if norm_cat not in cat_totals:
            w = cat_weights.get(norm_cat, a.get("weight"))
            cat_totals[norm_cat] = {"e": 0.0, "t": 0.0, "w": w}
        cat_totals[norm_cat]["e"] += float(a["earned"])
        cat_totals[norm_cat]["t"] += float(a["total"])

    wsum = 0.0
    wtot = 0.0
    for v in cat_totals.values():
        if v["w"] is not None and v["t"] > 0:
            wsum += (v["e"] / v["t"]) * v["w"]
            wtot += v["w"]
    return round((wsum / wtot) * 100, 2) if wtot > 0 else None


#  routes

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
    st = _state()
    if not st or not st["courses"]:
        return jsonify({"error": "not logged in"}), 401
    c    = st["courses"][idx]
    data = request.get_json(force=True)

    client_assignments = data.get("assignments") or []
    client_categories  = data.get("categories")  or c.get("categories") or []

    synthetic = {
        "assignments":    client_assignments,
        "categories":     client_categories,
        "earned":         c.get("earned"),
        "total":          c.get("total"),
        "percent":        c.get("percent"),
        "group_weighted": c.get("group_weighted", False),
    }
    projected = compute_percent(synthetic)
    return jsonify({"projected": projected})


@app.route("/course/<int:idx>/final", methods=["POST"])
def final_calc(idx):
    st = _state()
    if not st or not st["courses"]:
        return jsonify({"error": "not logged in"}), 401
    c    = st["courses"][idx]
    data = request.get_json(force=True)

    desired = _num(data.get("desired"))
    weight  = _num(data.get("weight"))
    if desired is None or not weight or weight <= 0 or weight >= 100:
        return jsonify({"error": "Enter a valid desired grade and final weight (1–99)."}), 400

    current = _num(data.get("current"))
    if current is None:
        client_assignments = data.get("assignments") or []
        client_categories  = data.get("categories")  or c.get("categories") or []
        synthetic = {
            "assignments":    client_assignments,
            "categories":     client_categories,
            "earned":         c.get("earned"),
            "total":          c.get("total"),
            "percent":        c.get("percent"),
            "group_weighted": c.get("group_weighted", False),
        }
        current = compute_percent(synthetic)
    if current is None:
        return jsonify({"error": "No current grade available to calculate from."}), 400

    w      = weight / 100.0
    needed = (desired - current * (1 - w)) / w
    return jsonify({"current": round(current, 2), "needed": round(needed, 2)})


@app.route("/api/districts")
def api_districts():
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
