import os
import re
import html
import json
import time
import smtplib
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from pydantic import BaseModel
from google import genai
from google.genai import types

# Modelos oficiales soportados en el tier gratuito
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-DO,es;q=0.9,en;q=0.8",
}

LINKEDIN_QUERIES = [
    "Python OR Backend OR FastAPI OR Django",
    "Software Developer OR Desarrollador OR Full Stack",
    "Analista de Datos OR QA OR Odoo OR Power Platform",
]

COMPUTRABAJO_URLS = [
    "https://do.computrabajo.com/trabajo-de-desarrollador-en-santo-domingo",
    "https://do.computrabajo.com/trabajo-de-programador-en-santo-domingo",
    "https://do.computrabajo.com/trabajo-de-analista-de-datos-en-santo-domingo",
]

EXCLUDE_TITLE_KEYWORDS = [
    "cctv", "camaras", "cámaras", "chofer", "vendedor", "contable", "conserje",
    "senior", "sr.", "sr ", "lead", "principal", "staff", "director", "gerente",
]

TECH_KEYWORDS = [
    "Python", "FastAPI", "Django", "Flask", "SQL", "PostgreSQL", "MySQL",
    "SQL Server", "Oracle", "Docker", "Git", "Linux", "AWS", "Azure",
    "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node.js",
    "Next.js", "HTML", "CSS", "REST", "API", "GraphQL", "Odoo", "ERP",
    "Power Platform", "Power BI", "Power Apps", "Power Automate", "SharePoint",
    "RPA", "Selenium", "Cypress", "Playwright", "QA", "Testing", "Scrum",
    "C#", ".NET", "Java", "Spring", "PHP", "Laravel", "Go", "Kotlin", "ETL",
]

# --- 1. MODELOS DE DATOS ---
class JobPosting(BaseModel):
    title: str
    company: str
    location: str
    salary: str
    salary_below_30k: Optional[bool] = None
    experience_requirements: str
    key_technologies: str
    url: str

class JobList(BaseModel):
    vacancies: list[JobPosting]

# --- 2. EXTRACCIÓN WEB DIRECTA DE PORTALES EN RD + ESTRUCTURADO CON FALLBACK ---
def clean_html_text(raw: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw or "")
    return html.unescape(re.sub(r"\s+", " ", no_tags)).strip()

def fetch_linkedin_rd_jobs() -> list[dict]:
    api_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    items: list[dict] = []
    seen_urls: set[str] = set()

    for kw in LINKEDIN_QUERIES:
        try:
            resp = requests.get(
                api_url,
                params={
                    "keywords": kw,
                    "location": "Dominican Republic",
                    "f_TPR": "r604800",  # Últimos 7 días
                },
                headers=HEADERS,
                timeout=12,
            )
            if resp.status_code != 200:
                continue

            cards = re.findall(r"<li[^>]*>(.*?)</li>", resp.text, re.S)
            for card in cards:
                t_match = re.search(r'base-search-card__title">\s*(.*?)\s*</h3>', card, re.S)
                c_match = re.search(
                    r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*(.*?)\s*(?:</a>)?\s*</h4>',
                    card,
                    re.S,
                )
                loc_match = re.search(r'job-search-card__location">\s*(.*?)\s*</span>', card, re.S)
                url_match = re.search(r'href="(https://[^"]+/jobs/view/[^?"]+)', card)

                if not t_match or not url_match:
                    continue

                title = clean_html_text(t_match.group(1))
                company = clean_html_text(c_match.group(1)) if c_match else "Empresa en LinkedIn RD"
                location = clean_html_text(loc_match.group(1)) if loc_match else "República Dominicana"
                url = url_match.group(1).strip()

                if url in seen_urls:
                    continue
                if any(bad in title.lower() for bad in EXCLUDE_TITLE_KEYWORDS):
                    continue

                seen_urls.add(url)
                items.append({
                    "title": title,
                    "company": company,
                    "location": location,
                    "salary": "No especificado en el anuncio",
                    "url": url,
                    "source": "LinkedIn RD",
                })
        except Exception as e:
            print(f"⚠️ Aviso consultando LinkedIn RD ({kw[:25]}...): {e}")

    return items

def fetch_computrabajo_rd_jobs() -> list[dict]:
    items: list[dict] = []
    seen_urls: set[str] = set()

    for page_url in COMPUTRABAJO_URLS:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue

            articles = re.findall(
                r'<article[^>]*class="[^"]*box_offer[^"]*"[^>]*>(.*?)</article>',
                resp.text,
                re.S,
            )
            for art in articles:
                link_m = re.search(
                    r'<a[^>]*class="[^"]*js-o-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    art,
                    re.S,
                )
                if not link_m:
                    continue

                rel_href = link_m.group(1).split("#")[0].strip()
                url = (
                    rel_href
                    if rel_href.startswith("http")
                    else f"https://do.computrabajo.com{rel_href}"
                )
                title = clean_html_text(link_m.group(2))

                if url in seen_urls:
                    continue
                if any(bad in title.lower() for bad in EXCLUDE_TITLE_KEYWORDS):
                    continue

                comp_m = re.search(
                    r"offer-grid-article-company-url[^>]*>(.*?)</a>", art, re.S
                )
                if not comp_m:
                    comp_m = re.search(
                        r'<p[^>]*class="[^"]*dFlex[^"]*"[^>]*>(.*?)</p>', art, re.S
                    )
                company = (
                    clean_html_text(comp_m.group(1))
                    if comp_m
                    else "Empresa en Computrabajo RD"
                )

                loc_m = re.search(
                    r'<p[^>]*class="fs16 fc_base mt5"[^>]*>\s*<span[^>]*>(.*?)</span>',
                    art,
                    re.S,
                )
                location = (
                    clean_html_text(loc_m.group(1))
                    if loc_m
                    else "Santo Domingo, RD"
                )

                sal_m = re.search(
                    r'class="icon i_salary"[^>]*></span>\s*([^<]+)', art, re.S
                )
                salary = (
                    f"RD$ {clean_html_text(sal_m.group(1))}"
                    if sal_m
                    else "No especificado en el anuncio"
                )

                seen_urls.add(url)
                items.append({
                    "title": title,
                    "company": company,
                    "location": location,
                    "salary": salary,
                    "url": url,
                    "source": "Computrabajo RD",
                })
        except Exception as e:
            print(f"⚠️ Aviso consultando Computrabajo RD ({page_url}): {e}")

    return items

def detect_technologies(text: str) -> str:
    found = []
    for tech in TECH_KEYWORDS:
        pattern = r"(?<!\w)" + re.escape(tech) + r"(?!\w)"
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.append(tech)
    return ", ".join(found) if found else "Desarrollo de Software / TI"

def parse_results_locally(raw_items: list[dict]) -> list[JobPosting]:
    print("🛠️ Aplicando extractor estructurado local (respaldo anti-429)...")
    postings: list[JobPosting] = []

    for item in raw_items:
        title = item.get("title", "")
        company = item.get("company", "No especificada")
        location = item.get("location", "Santo Domingo, RD")
        salary = item.get("salary", "No especificado en el anuncio")
        url = item.get("url", "")
        source = item.get("source", "Portal de Empleo RD")

        combined_text = f"{title} {company} {location}"
        if re.search(r"\b(remoto|remote)\b", combined_text, flags=re.IGNORECASE) and "remoto" not in location.lower():
            location = f"{location} (Remoto)"

        salary_below_30k = None
        if salary and salary != "No especificado en el anuncio":
            num_match = re.search(r"([\d,]+(?:\.\d+)?)", salary)
            if num_match:
                val = float(num_match.group(1).replace(",", ""))
                if val > 0:
                    salary_below_30k = val < 30000

        techs = detect_technologies(f"{title} {url}")
        reqs = f"Vacante activa en {source} para el rol de {title}. Revisar enlace para detalles y postulación."

        postings.append(
            JobPosting(
                title=title,
                company=company,
                location=location,
                salary=salary,
                salary_below_30k=salary_below_30k,
                experience_requirements=reqs,
                key_technologies=techs,
                url=url,
            )
        )

    return postings

def search_jobs(api_key: str) -> list[JobPosting]:
    print("🔎 Paso 1: Obteniendo vacantes activas en LinkedIn RD y Computrabajo RD...")
    raw_items = fetch_linkedin_rd_jobs() + fetch_computrabajo_rd_jobs()

    # Deduplicar por URL y por (título, empresa) manteniendo orden
    unique_items: list[dict] = []
    seen: set[str] = set()
    seen_title_company: set[tuple[str, str]] = set()
    for item in raw_items:
        u = item["url"]
        tc_key = (item["title"].lower().strip(), item["company"].lower().strip())
        if u not in seen and tc_key not in seen_title_company:
            seen.add(u)
            seen_title_company.add(tc_key)
            unique_items.append(item)

    print(f"✅ Búsqueda web completada: {len(unique_items)} vacantes activas encontradas.")
    if not unique_items:
        return []

    raw_search_text = json.dumps(unique_items, ensure_ascii=False, indent=2)

    # Paso 2: Intentar estructurar y enriquecer con Gemini (sin tool google_search = mucho menor consumo de cuota)
    print("📋 Paso 2: Estructurando resultados en formato JSON con Gemini + Pydantic...")
    client = genai.Client(api_key=api_key)
    parse_prompt = (
        "Convert and enrich the Dominican Republic software/IT job vacancies below into the requested JSON schema.\n"
        "Rules:\n"
        "- Keep only software, development, data, QA, automation, ERP/Odoo, or IT roles.\n"
        "- Set salary_below_30k to true ONLY if confirmed monthly salary is under RD$30,000; "
        "false if >= RD$30,000; null if undisclosed.\n"
        "- Infer key_technologies and a concise Spanish summary for experience_requirements from the job title and URL slug.\n"
        "- Preserve the exact URL from each item.\n\n"
        f"Raw vacancies:\n{raw_search_text}"
    )

    for model_name in FALLBACK_MODELS:
        try:
            print(f"🤖 Estructurando JSON con {model_name}...")
            if model_name.startswith("gemma-"):
                schema_hint = json.dumps(JobList.model_json_schema(), ensure_ascii=False)
                parse_response = client.models.generate_content(
                    model=model_name,
                    contents=f"{parse_prompt}\n\nRespond ONLY with valid JSON matching this schema:\n{schema_hint}",
                    config=types.GenerateContentConfig(temperature=0.1),
                )
            else:
                parse_response = client.models.generate_content(
                    model=model_name,
                    contents=parse_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=JobList,
                        temperature=0.1,
                    ),
                )

            if not parse_response.text:
                raise ValueError(f"El modelo {model_name} devolvió una respuesta vacía.")

            clean_json = parse_response.text.strip()
            if clean_json.startswith("```"):
                clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean_json, flags=re.DOTALL).strip()

            parsed = JobList.model_validate_json(clean_json)
            if parsed.vacancies:
                print(f"✅ JSON estructurado con éxito usando {model_name}.")
                return parsed.vacancies
        except Exception as e:
            print(f"⚠️ Falló {model_name}: {e}")
            time.sleep(2)

    print("⚠️ Cuota de API Gemini agotada en todos los modelos. Usando estructurado local de respaldo...")
    return parse_results_locally(unique_items)


# --- 3. GESTION DE ESTADO (DEDUPLICACION) ---
def load_seen_urls(history_file="seen_jobs.json") -> set[str]:
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()

def filter_new_vacancies(vacancies: list[JobPosting], history_file="seen_jobs.json") -> list[JobPosting]:
    seen_urls = load_seen_urls(history_file)
    new_items = []
    batch_urls = set()

    for job in vacancies:
        url = job.url.strip() if job.url else ""
        if url and url not in seen_urls and url not in batch_urls:
            batch_urls.add(url)
            new_items.append(job)

    return new_items

def save_seen_vacancies(new_jobs: list[JobPosting], history_file="seen_jobs.json"):
    seen_urls = load_seen_urls(history_file)
    seen_urls.update(job.url.strip() for job in new_jobs if job.url)
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen_urls)), f, indent=2, ensure_ascii=False)

# --- 4. ENVIO DE CORREO SMTP ---
def send_email_alert(new_jobs: list[JobPosting]):
    sender = os.environ["EMAIL_SENDER"]
    receiver = os.environ["EMAIL_RECEIVER"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    rows = []
    for job in new_jobs:
        alert_tag = (
            "<span style='color: #d93025; font-weight: bold;'> [⚠️ &lt; RD$30k]</span>"
            if job.salary_below_30k
            else ""
        )
        safe_url = html.escape(job.url.strip(), quote=True)
        safe_title = html.escape(job.title)
        safe_company = html.escape(job.company)
        safe_location = html.escape(job.location)
        safe_salary = html.escape(job.salary)
        safe_reqs = html.escape(job.experience_requirements)
        safe_stack = html.escape(job.key_technologies)

        rows.append(f"""
        <div style="padding: 14px; border-bottom: 1px solid #e0e0e0; font-family: sans-serif;">
            <h3 style="margin: 0 0 6px 0;">
                <a href="{safe_url}" style="color: #1a73e8; text-decoration: none;">{safe_title}</a>
            </h3>
            <p style="margin: 2px 0; color: #333;"><strong>Empresa:</strong> {safe_company} | <strong>Ubicación:</strong> {safe_location}</p>
            <p style="margin: 2px 0; color: #333;"><strong>Salario:</strong> {safe_salary}{alert_tag}</p>
            <p style="margin: 2px 0; color: #555;"><strong>Requisitos:</strong> {safe_reqs}</p>
            <p style="margin: 2px 0; color: #555;"><strong>Stack:</strong> <code>{safe_stack}</code></p>
        </div>
        """)

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; max-width: 650px; margin: auto; padding: 20px;">
        <h2 style="color: #202124;">🎯 {len(new_jobs)} Nueva(s) Vacante(s) Encontrada(s)</h2>
        {''.join(rows)}
      </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📢 {len(new_jobs)} nueva(s) vacante(s) de desarrollo en RD"
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, receiver, msg.as_string())

# --- 5. PUNTO DE ENTRADA ---
if __name__ == "__main__":
    required_vars = ["GEMINI_API_KEY", "EMAIL_SENDER", "EMAIL_RECEIVER", "EMAIL_APP_PASSWORD"]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        raise ValueError(f"Faltan configurar las variables de entorno: {', '.join(missing_vars)}")

    api_key = os.environ["GEMINI_API_KEY"]

    print("🔎 Buscando vacantes activas...")
    all_jobs = search_jobs(api_key)
    print(f"Total de vacantes procesadas: {len(all_jobs)}")

    new_jobs = filter_new_vacancies(all_jobs)

    if new_jobs:
        print(f"🚀 {len(new_jobs)} vacantes no repetidas. Enviando notificación...")
        send_email_alert(new_jobs)
        save_seen_vacancies(new_jobs)
        print("✅ Correo enviado e historial actualizado con éxito.")
    else:
        print("🤫 No hay vacantes nuevas o ya fueron notificadas. Finalizando en silencio.")

