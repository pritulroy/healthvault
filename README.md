# HealthVault

**Your Health. Your Identity. Your Control.**

HealthVault is a patient-controlled digital health profile system. Version 1
focuses on one thing: an **Emergency Health Profile with QR code access**.

A user creates an account, enters key emergency information (blood group,
allergies, and so on), and receives a unique QR code. If that person is
involved in an accident, anyone can scan the QR code, **without logging in**,
and see only the emergency information the user chose to share.

> **Prototype notice:** HealthVault is a prototype for educational and
> demonstration purposes. Do not upload real sensitive medical information to
> this demo deployment.

## Main features

- Landing page, registration, login, and logout
- Personal dashboard where each user edits only their own profile
- Emergency Health Profile: blood group, allergies, current medication,
  medical condition, emergency contact, organ donor status, preferred language
- Unique QR code generated in Python, with a **Download QR Code** button
- Public, mobile-friendly emergency page with a tap-to-call contact button
- **Emergency QR Status** switch (Active / Disabled)
- **Create New QR Code** button that invalidates a lost or leaked QR code
- Friendly error pages and a privacy notice on every page
## Technology used

| Area | Tools |
|---|---|
| Backend | Python 3, Flask |
| Database | SQLite via Flask-SQLAlchemy |
| Security | Werkzeug password hashing, Flask-WTF (CSRF), Flask sessions |
| QR codes | `qrcode` + Pillow |
| Frontend | HTML5, CSS3, JavaScript |
| Production server | Gunicorn |

## Project structure

```text
healthvault/
 ├── app.py
 ├── requirements.txt
 ├── README.md
 ├── .gitignore
 ├── templates/
 │   ├── base.html
 │   ├── index.html
 │   ├── register.html
 │   ├── login.html
 │   ├── dashboard.html
 │   ├── emergency.html
 │   └── error.html
 └── static/
     ├── css/style.css
     └── js/script.js

## How to install

You need Python 3.10 or newer.

```bash
git clone https://github.com/pritulroy/healthvault.git
cd healthvault
python -m venv venv

# Windows:
venv\Scripts\activate

# Mac/Linux:
source venv/bin/activate

pip install -r requirements.txt
## How to run locally

```bash
python app.py

### Testing the QR code from your phone

The QR code points to the server's address. On your own PC that is
`127.0.0.1`, which a phone cannot reach.

To test on a phone on the same Wi-Fi, set `BASE_URL` to your PC's local IP
and run:

```bash
# Mac/Linux
export BASE_URL=http://192.168.1.25:5000
flask --app app run --host=0.0.0.0
## Environment variables

| Variable | Used for |
|---|---|
| `SECRET_KEY` | Signs session cookies. **Required in production.** |
| `APP_ENV` | Set to `production` on the real server |
| `BASE_URL` | Public site address used inside QR codes |
| `DATABASE_URL` | Optional. Defaults to local SQLite. |

Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
## How QR emergency access works

```text
QR code
   ↓
https://your-site/emergency/<random-token>
   ↓
Server looks up the profile that owns the token
   ↓
Active?  → show ONLY the emergency fields
Disabled → "This emergency profile is currently unavailable."
Unknown  → "Profile Not Found"
## Database structure

### users

| Column | Type | Notes |
|---|---|---|
| id | Integer | Primary key |
| full_name | String(100) | |
| email | String(120) | Unique |
| password_hash | String(255) | Never the plain password |
| created_at | DateTime | |

### emergency_profiles

| Column | Type | Notes |
|---|---|---|
| id | Integer | Primary key |
| user_id | Integer | Foreign key to users.id, unique |
| blood_group | String(10) | |
| allergies | Text | |
| medications | Text | |
| medical_condition | Text | |
| emergency_contact_name | String(100) | |
| emergency_contact_phone | String(30) | |
| organ_donor | String(10) | Yes / No / empty |
| preferred_language | String(50) | |
| emergency_token | String(64) | Unique, random |
| qr_enabled | Boolean | Active / Disabled |
| updated_at | DateTime | |

`User.profile` and `EmergencyProfile.user` form a one-to-one relationship.
## Security considerations

Implemented:

- Passwords hashed with Werkzeug
- CSRF protection on every form
- Signed, HttpOnly, SameSite session cookies
- Secure cookies in production
- Sessions expire after 2 hours
- Login lockout after repeated failed attempts
- Generic login errors
- Server-side validation
- Request size limit
- Content-Security-Policy
- X-Frame-Options
- nosniff
- HSTS headers
- Emergency pages are not cached
- Emergency pages ask search engines not to index them
- Users can only access their own private data

### Known limitations

A real medical product would additionally need:

- Email verification and password reset
- Audit log of who viewed a profile
- Encryption of data at rest
- Persistent, shared rate limiting
- Database migrations
- Legal and privacy review

Anyone who holds the QR link can see the emergency profile. That is the
purpose of the feature, but users should share only information they are
comfortable with.
## Future features

Possible future versions:

- Medical document storage
- Prescription history
- Laboratory reports
- Medical timeline
- Doctor sharing
- Temporary access links
- AI-assisted document organization

## License

Educational project. Add a license of your choice before reusing the code.