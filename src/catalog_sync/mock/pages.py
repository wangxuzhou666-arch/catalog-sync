"""The HTML the Mock SaaS serves: a login form, a small app that calls its JSON API, the terms.

The app does what a single-page web app does: after login it keeps the bearer token and sends it
with every `fetch`. That is the request a browser-based login (`catalog-sync login mock
--browser`) watches for to capture the token. Nothing here loads anything from outside; values
from the API are put on the page with `textContent`, never as markup.
"""

from __future__ import annotations

_STYLE = """
  body { font: 16px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 46rem;
         padding: 0 1rem; }
  label { display: block; margin: .8rem 0 .2rem; }
  input { font: inherit; padding: .4rem; width: 16rem; }
  button { font: inherit; padding: .4rem 1rem; margin-top: 1rem; }
  table { border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }
  th, td { border-bottom: 1px solid #8884; padding: .3rem .5rem; text-align: left; }
  .note { color: #666; font-size: .9rem; }
  .error { color: #b00020; }
"""


def login_page() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mock SaaS: sign in</title>
<style>{_STYLE}</style></head>
<body>
<h1>Mock SaaS</h1>
<p class="note">A fake service for trying catalog-sync. Everything here is invented.
Demo account: see the README (it is a demo-only default).</p>
<form id="login-form">
  <label for="username">User name</label>
  <input id="username" name="username" autocomplete="username" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <div><button id="sign-in" type="submit">Sign in</button></div>
  <p id="message" class="error"></p>
</form>
<script>
document.getElementById("login-form").addEventListener("submit", async (event) => {{
  event.preventDefault();
  const reply = await fetch("/api/login", {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    }}),
  }});
  const body = await reply.json();
  if (!reply.ok) {{
    document.getElementById("message").textContent = body.msg;
    return;
  }}
  localStorage.setItem("mock_token", body.data.token);
  location.href = "/app";
}});
</script>
</body></html>
"""


def app_page() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mock SaaS: library</title>
<style>{_STYLE}</style></head>
<body>
<h1>Mock SaaS library</h1>
<p class="note"><a href="/terms">Terms</a> &middot; <a href="/login" id="sign-out">Sign out</a></p>
<h2>Materials (root folder)</h2>
<table id="materials"><thead><tr><th>Code</th><th>Name</th></tr></thead><tbody></tbody></table>
<h2>Suppliers</h2>
<table id="suppliers"><thead><tr><th>Name</th><th>Country</th></tr></thead><tbody></tbody></table>
<p id="message" class="error"></p>
<script>
const token = localStorage.getItem("mock_token");
document.getElementById("sign-out").addEventListener(
  "click", () => localStorage.removeItem("mock_token"));

async function call(path) {{
  const reply = await fetch(path, {{headers: {{authorization: "Bearer " + token}}}});
  if (reply.status === 401) {{
    localStorage.removeItem("mock_token");
    location.href = "/login";
    throw new Error("signed out");
  }}
  return (await reply.json()).data;
}}

function fill(tableId, rows, columns) {{
  const body = document.querySelector("#" + tableId + " tbody");
  for (const row of rows) {{
    const line = body.insertRow();
    for (const column of columns) {{ line.insertCell().textContent = row[column]; }}
  }}
}}

// Without a token the page goes to the login form and asks the API for nothing: a request
// sent as "Bearer null" would only be refused, and a login capture must not mistake it for one.
if (!token) {{
  location.href = "/login";
}} else {{
  call("/api/v1/materials?folder_id=0&page=1&per_page=10").then((data) =>
    fill("materials", data.rows.filter((row) => row.type === "material"), ["code", "name"]));
  call("/api/v1/suppliers?limit=10").then((data) =>
    fill("suppliers", data.items, ["name", "country"]));
}}
</script>
</body></html>
"""


def terms_page() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mock SaaS: terms</title>
<style>{_STYLE}</style></head>
<body>
<h1>Terms of use (demo)</h1>
<p>This service is a fake, run on your own machine by catalog-sync for demonstration and testing.
It holds invented data only. You may read all of it, as often as the rate limit allows, and copy
it anywhere; there is no one to ask and nothing to protect.</p>
<p class="note">Pipelines that read a real service declare the terms they reviewed in their
compliance block. For this one, that is this page.</p>
</body></html>
"""
