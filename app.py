import io
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from functools import wraps

import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, g,
    send_file
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect, CSRFError
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------
# App setup
# ---------------------------------------------------------------
app = Flask(__name__)

# Set APP_ENV=production on the real server (we do this in Step 11).
# Do NOT set it on your own computer: production mode expects HTTPS.
IS_PRODUCTION = os.environ.get("APP_ENV") == "production"

secret_key = os.environ.get("SECRET_KEY")
if IS_PRODUCTION and not secret_key:
    # Refuse to start with the well-known development key on a real server.
    raise RuntimeError("SECRET_KEY must be set when APP_ENV=production.")
app.config["SECRET_KEY"] = secret_key or "dev-only-change-me"

# Session cookie settings
app.config["SESSION_COOKIE_HTTPONLY"] = True            # JavaScript cannot read it
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"           # not sent on cross-site POSTs
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION     # HTTPS only on the real server
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)   # auto logout

# CSRF tokens stay valid as long as the session does
app.config["WTF_CSRF_TIME_LIMIT"] = None

# Reject any request larger than 64 KB.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///healthvault.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# On Render (and similar hosts) a proxy sits in front of our app. ProxyFix
# lets Flask see the visitor's real IP address and that the site uses https.
if IS_PRODUCTION:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)     # checks a secret token on every POST request


# ---------------------------------------------------------------
# Helper functions and constants
# ---------------------------------------------------------------
def utc_now():
    """Current time in UTC. Used as the default for date columns."""
    return datetime.now(timezone.utc)


def generate_token():
    """Create a long, random, unpredictable token for the emergency URL."""
    return secrets.token_urlsafe(32)


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Phone numbers: digits, spaces, + - ( ) only
PHONE_PATTERN = re.compile(r"^[0-9+\-()\s]{3,30}$")

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]
ORGAN_DONOR_OPTIONS = ["Yes", "No"]

MAX_LONG_TEXT = 1000     # allergies, medications, medical condition
MAX_NAME = 100
MAX_LANGUAGE = 50
MAX_PASSWORD = 128       # stops absurdly long passwords from slowing the server

# A hash of a password nobody knows. We check against it when an email does
# not exist, so "unknown email" takes as long as "wrong password".
DUMMY_HASH = generate_password_hash("not-a-real-password")


# ---------------------------------------------------------------
# Login lockout (simple, in memory)
# ---------------------------------------------------------------
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60

# Maps "ip|email" to a list of failed-attempt timestamps.
# It lives in memory, so it resets when the server restarts.
failed_logins = {}


def login_key(email):
    return f"{request.remote_addr}|{email}"


def is_locked_out(key):
    """True if this ip+email had too many failures in the last 15 minutes."""
    now = time.time()
    recent = [t for t in failed_logins.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    if recent:
        failed_logins[key] = recent
    else:
        failed_logins.pop(key, None)
    return len(recent) >= LOGIN_MAX_ATTEMPTS


def record_failed_login(key):
    # Safety valve so this dictionary can never grow without limit.
    if len(failed_logins) > 10000:
        failed_logins.clear()
    failed_logins.setdefault(key, []).append(time.time())


def clear_failed_logins(key):
    failed_logins.pop(key, None)


# ---------------------------------------------------------------
# Database models
# ---------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    profile = db.relationship(
        "EmergencyProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def set_password(self, password):
        """Hash the password. We NEVER store the plain-text password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Return True if the password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.email}>"


class EmergencyProfile(db.Model):
    __tablename__ = "emergency_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False
    )

    blood_group = db.Column(db.String(10))
    allergies = db.Column(db.Text)
    medications = db.Column(db.Text)
    medical_condition = db.Column(db.Text)
    emergency_contact_name = db.Column(db.String(100))
    emergency_contact_phone = db.Column(db.String(30))
    organ_donor = db.Column(db.String(10))          # "Yes", "No", or empty
    preferred_language = db.Column(db.String(50))

    emergency_token = db.Column(
        db.String(64), unique=True, nullable=False, default=generate_token
    )
    qr_enabled = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship("User", back_populates="profile")

    def __repr__(self):
        return f"<EmergencyProfile user_id={self.user_id}>"


with app.app_context():
    db.create_all()


# ---------------------------------------------------------------
# QR code helpers
# ---------------------------------------------------------------
def get_emergency_url(profile):
    """
    Build the full public URL that the QR code points to.

    If a BASE_URL environment variable is set (for example
    https://healthvault.onrender.com), we use it. Otherwise Flask works out
    the address from the current request (http://127.0.0.1:5000 locally).
    """
    path = url_for("emergency_profile", token=profile.emergency_token)
    base_url = os.environ.get("BASE_URL")
    if base_url:
        return base_url.rstrip("/") + path
    return url_for(
        "emergency_profile", token=profile.emergency_token, _external=True
    )


def build_qr_png(url):
    """
    Create a QR code image for `url` and return it as PNG bytes in memory.
    Nothing is written to disk. The QR contains ONLY the URL, never any
    medical information.
    """
    qr = qrcode.QRCode(
        version=None,                                        # pick the smallest size that fits
        error_correction=qrcode.constants.ERROR_CORRECT_M,   # survives some smudging/damage
        box_size=10,                                         # pixels per square
        border=4,                                            # white margin (needed for scanning)
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------
# Login helpers and security headers
# ---------------------------------------------------------------
@app.before_request
def load_logged_in_user():
    """Runs before EVERY request. Loads the logged-in user into g.user."""
    user_id = session.get("user_id")
    g.user = db.session.get(User, user_id) if user_id else None

    if user_id and g.user is None:
        session.clear()


# Content-Security-Policy: tells the browser to load ONLY our own files.
# Even if someone managed to inject a <script> tag, the browser would refuse
# to run it. We have no inline scripts or styles, so this is safe to enforce.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@app.after_request
def add_security_headers(response):
    """Add protective headers to every response."""
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"      # no MIME guessing
    response.headers["X-Frame-Options"] = "DENY"                # cannot be put in an iframe
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    if IS_PRODUCTION:
        # Tell browsers to use HTTPS only for this site for the next year.
        response.headers["Strict-Transport-Security"] = "max-age=31536000"

    if request.endpoint in (
        "dashboard", "emergency_profile", "qr_image", "qr_download"
    ):
        # Do not let the browser store these pages or images.
        response.headers["Cache-Control"] = "no-store"

    if request.endpoint == "emergency_profile":
        # Ask search engines not to index emergency pages.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        # Do not tell other sites which emergency URL a visitor came from.
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def login_required(view):
    """Decorator: logged-out visitors are sent to the login page."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Please log in to access your dashboard.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


# ---------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------
def render_error(code, title, message):
    """Show our friendly error page with the right HTTP status code."""
    return render_template(
        "error.html", code=code, title=title, message=message
    ), code


@app.errorhandler(CSRFError)
def csrf_error(error):
    # Missing or wrong CSRF token (expired session, or a forged request).
    return render_error(
        400, "Request Not Verified",
        "Your session may have expired, or the form could not be verified. "
        "Please go back, refresh the page, and try again."
    )


@app.errorhandler(403)
def forbidden(error):
    return render_error(
        403, "Access Denied", "You do not have permission to view this page."
    )


@app.errorhandler(404)
def page_not_found(error):
    return render_error(
        404, "Page Not Found",
        "We could not find the page you were looking for."
    )


@app.errorhandler(405)
def method_not_allowed(error):
    return render_error(
        405, "Not Allowed", "That action is not allowed on this page."
    )


@app.errorhandler(413)
def too_large(error):
    return render_error(
        413, "Request Too Large",
        "The information you sent was too large. Please shorten it and try again."
    )


@app.errorhandler(500)
def server_error(error):
    db.session.rollback()
    return render_error(
        500, "Something Went Wrong",
        "An unexpected error happened on our side. Please try again in a moment."
    )


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
@app.route("/")
def index():
    """Landing page."""
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    """Show the registration form (GET) and process it (POST)."""
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        errors = []

        if not full_name or not email or not password or not confirm_password:
            errors.append("Please fill in all fields.")
        else:
            if len(full_name) > MAX_NAME:
                errors.append("Full name must be 100 characters or fewer.")
            if not EMAIL_PATTERN.match(email) or len(email) > 120:
                errors.append("Please enter a valid email address.")
            if len(password) < 8:
                errors.append("Password must be at least 8 characters long.")
            if len(password) > MAX_PASSWORD:
                errors.append("Password must be 128 characters or fewer.")
            if password != confirm_password:
                errors.append("Passwords do not match.")

        if not errors and User.query.filter_by(email=email).first():
            errors.append("An account with this email already exists.")

        if errors:
            for error in errors:
                flash(error, "error")
            return render_template(
                "register.html", full_name=full_name, email=email
            )

        user = User(full_name=full_name, email=email)
        user.set_password(password)
        user.profile = EmergencyProfile()

        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An account with this email already exists.", "error")
            return render_template(
                "register.html", full_name=full_name, email=email
            )

        flash("Account created successfully! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", full_name="", email="")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Show the login form (GET) and check the credentials (POST)."""
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter your email and password.", "error")
            return render_template("login.html", email=email)

        # Same message as a wrong password. Also avoids hashing huge inputs.
        if len(password) > MAX_PASSWORD or len(email) > 120:
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email[:120])

        # Too many recent failures for this ip + email? Stop here.
        key = login_key(email)
        if is_locked_out(key):
            flash(
                "Too many failed login attempts. "
                "Please wait 15 minutes and try again.",
                "error",
            )
            return render_template("login.html", email=email), 429

        user = User.query.filter_by(email=email).first()

        if user is None:
            check_password_hash(DUMMY_HASH, password)   # keep timing the same
            valid = False
        else:
            valid = user.check_password(password)

        if not valid:
            record_failed_login(key)
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email)

        clear_failed_logins(key)
        session.clear()
        session.permanent = True        # uses PERMANENT_SESSION_LIFETIME (2 hours)
        session["user_id"] = user.id

        flash("Logged in successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html", email="")


@app.route("/logout", methods=["POST"])
def logout():
    """Log the user out (POST only, so other sites cannot trigger it)."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


def profile_to_form(user):
    """Turn a user's saved data into a simple dictionary for the template."""
    profile = user.profile
    return {
        "full_name": user.full_name,
        "blood_group": profile.blood_group or "",
        "allergies": profile.allergies or "",
        "medications": profile.medications or "",
        "medical_condition": profile.medical_condition or "",
        "emergency_contact_name": profile.emergency_contact_name or "",
        "emergency_contact_phone": profile.emergency_contact_phone or "",
        "organ_donor": profile.organ_donor or "",
        "preferred_language": profile.preferred_language or "",
    }


def render_dashboard(user, form):
    """
    Render the dashboard page. Both the GET and the "validation failed" paths
    need the same extra values, so we collect them in one place.
    """
    return render_template(
        "dashboard.html",
        form=form,
        profile=user.profile,
        blood_groups=BLOOD_GROUPS,
        organ_donor_options=ORGAN_DONOR_OPTIONS,
        emergency_url=get_emergency_url(user.profile),
    )


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    """
    Show the Emergency Health Profile form (GET) and save it (POST).
    We always use g.user (from the session), never an id from the URL.
    """
    user = g.user

    if user.profile is None:
        user.profile = EmergencyProfile()
        db.session.commit()

    if request.method == "POST":
        form = {
            "full_name": request.form.get("full_name", "").strip(),
            "blood_group": request.form.get("blood_group", "").strip(),
            "allergies": request.form.get("allergies", "").strip(),
            "medications": request.form.get("medications", "").strip(),
            "medical_condition": request.form.get("medical_condition", "").strip(),
            "emergency_contact_name": request.form.get("emergency_contact_name", "").strip(),
            "emergency_contact_phone": request.form.get("emergency_contact_phone", "").strip(),
            "organ_donor": request.form.get("organ_donor", "").strip(),
            "preferred_language": request.form.get("preferred_language", "").strip(),
        }

        errors = []

        if not form["full_name"]:
            errors.append("Full name cannot be empty.")
        elif len(form["full_name"]) > MAX_NAME:
            errors.append("Full name must be 100 characters or fewer.")

        if form["blood_group"] and form["blood_group"] not in BLOOD_GROUPS:
            errors.append("Please choose a valid blood group.")
        if form["organ_donor"] and form["organ_donor"] not in ORGAN_DONOR_OPTIONS:
            errors.append("Please choose a valid organ donor option.")

        for field, label in [
            ("allergies", "Allergies"),
            ("medications", "Important current medication"),
            ("medical_condition", "Important medical condition"),
        ]:
            if len(form[field]) > MAX_LONG_TEXT:
                errors.append(f"{label} must be {MAX_LONG_TEXT} characters or fewer.")

        if len(form["emergency_contact_name"]) > MAX_NAME:
            errors.append("Emergency contact name must be 100 characters or fewer.")
        if len(form["preferred_language"]) > MAX_LANGUAGE:
            errors.append("Preferred language must be 50 characters or fewer.")

        if form["emergency_contact_phone"] and not PHONE_PATTERN.match(
            form["emergency_contact_phone"]
        ):
            errors.append(
                "Emergency contact phone may only contain numbers, spaces, "
                "+, -, and parentheses."
            )

        if errors:
            for error in errors:
                flash(error, "error")
            return render_dashboard(user, form)

        profile = user.profile
        user.full_name = form["full_name"]
        profile.blood_group = form["blood_group"] or None
        profile.allergies = form["allergies"] or None
        profile.medications = form["medications"] or None
        profile.medical_condition = form["medical_condition"] or None
        profile.emergency_contact_name = form["emergency_contact_name"] or None
        profile.emergency_contact_phone = form["emergency_contact_phone"] or None
        profile.organ_donor = form["organ_donor"] or None
        profile.preferred_language = form["preferred_language"] or None

        db.session.commit()

        flash("Your emergency profile has been saved.", "success")
        return redirect(url_for("dashboard"))

    return render_dashboard(user, profile_to_form(user))


@app.route("/qr-status", methods=["POST"])
@login_required
def qr_status():
    """
    Turn the owner's emergency QR on or off.

    The form sends an explicit "enable" or "disable" instead of just
    "toggle", so an out-of-date page can never flip the status the wrong way.
    """
    action = request.form.get("action", "")
    profile = g.user.profile

    if action == "enable":
        profile.qr_enabled = True
        db.session.commit()
        flash("Your emergency QR is now ACTIVE. Scanning it shows your profile.", "success")
    elif action == "disable":
        profile.qr_enabled = False
        db.session.commit()
        flash("Your emergency QR is now DISABLED. Scanning it shows nothing.", "info")
    else:
        flash("Invalid request.", "error")

    return redirect(url_for("dashboard"))


@app.route("/qr-regenerate", methods=["POST"])
@login_required
def qr_regenerate():
    """
    Replace the secret token. The OLD QR code and link stop working
    immediately. Use this if a printed QR code is lost or shared by mistake.
    """
    g.user.profile.emergency_token = generate_token()
    db.session.commit()
    flash(
        "A new QR code was created. The old QR code no longer works, so "
        "download the new one and replace any printed copies.",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.route("/qr-code.png")
@login_required
def qr_image():
    """The logged-in user's QR code as an image (shown on the dashboard)."""
    url = get_emergency_url(g.user.profile)
    return send_file(build_qr_png(url), mimetype="image/png")


@app.route("/qr-code/download")
@login_required
def qr_download():
    """Same QR code, but sent as a file download."""
    url = get_emergency_url(g.user.profile)
    return send_file(
        build_qr_png(url),
        mimetype="image/png",
        as_attachment=True,
        download_name="healthvault-emergency-qr.png",
    )


@app.route("/emergency/<token>")
def emergency_profile(token):
    """
    PUBLIC page (no login). Anyone with the token URL can view it.

    We copy ONLY the emergency fields into a small dictionary and send that
    to the template, so the email, password hash, and ids can never leak.
    """
    profile = EmergencyProfile.query.filter_by(emergency_token=token).first()

    if profile is None:
        return render_template("emergency.html", state="not_found"), 404

    if not profile.qr_enabled:
        return render_template("emergency.html", state="disabled"), 403

    data = {
        "name": profile.user.full_name,
        "blood_group": profile.blood_group,
        "allergies": profile.allergies,
        "medications": profile.medications,
        "medical_condition": profile.medical_condition,
        "contact_name": profile.emergency_contact_name,
        "contact_phone": profile.emergency_contact_phone,
        "organ_donor": profile.organ_donor,
        "preferred_language": profile.preferred_language,
        "updated_at": profile.updated_at,
    }
    return render_template("emergency.html", state="active", data=data)


if __name__ == "__main__":
    app.run(debug=True)