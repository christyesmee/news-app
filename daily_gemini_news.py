import os
import requests
import smtplib
import traceback
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime, timedelta

# --- CONFIGURATION ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# --- SOURCES: COMPUTING & ASIAN SUPPLY CHAIN ---
SOURCES = [
    # Global Macro
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    
    # Asian Supply Chain (Critical for Compute)
    "scmp.com",          # South China Morning Post
    "nikkei.com",        # Nikkei Asia
    "caixinglobal.com",  # China Finance
    "digitimes.com",     # Taiwan Electronics (Key Source)
    "taipeitimes.com",   # Taiwan General
    
    # Tech Industry Specific
    "techcrunch.com", "wired.com", "theregister.com", "anandtech.com"
]

def get_working_model():
    """Auto-selects the best available Gemini model."""
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        all_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for m in all_models:
            if "1.5-pro" in m: return m
        for m in all_models:
            if "1.5-flash" in m: return m
        return all_models[0] if all_models else None
    except Exception as e:
        print(f"!! Model selection error: {e}")
        return None

def fetch_compute_news():
    print("--- 1. Fetching Compute Market News ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    
    # QUERY LOGIC
    query = (
        '(semiconductor OR "AI chips" OR GPU OR "data center" OR foundry OR '
        '"supply chain" OR "rare earth" OR lithography OR "energy prices" OR '
        '"trade war" OR tariffs OR ASML OR TSMC OR NVIDIA) '
        'AND (China OR Taiwan OR US OR Global)'
    )
    
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': query,
        'domains': domains,
        'from': date_from,
        'sortBy': 'relevance',
        'language': 'en',
        'pageSize': 40,
        'apiKey': NEWS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} relevant articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name: return None, "No AI models available."

    print(f"--- 2. Analyzing with {model_name} ---")
    raw_text = ""
    for i, a in enumerate(articles[:30]):
        safe_title = a['title'].replace('"', "'")
        raw_text += f"ID: {i+1} | Title: {safe_title} | Source: {a['source']['name']} | URL: {a['url']}\n"

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # FIXED PROMPT SYNTAX (No line breaks inside strings)
    prompt = (
        "Role: You are a specialized Compute Market Strategist. Your client invests in Semiconductors, Data Centers, and AI Hardware.\n"
        "Goal: Identify exactly 3 critical market shifts. Focus on Direct impacts and Indirect impacts (e.g., Copper prices rising -> PCB costs up).\n"
        "Constraints: Use Chinese/Asian sources (SCMP, Nikkei, DigiTimes) to balance US narratives.\n\n"

        "### INSTRUCTIONS FOR LINKS:\n"
        "Cite sources using clickable HTML footnotes: <a href='URL_FROM_INPUT'>[1]</a>.\n\n"
        
        "### VISUALIZATION:\n"
        "Insert a placeholder tag"
        "Example: [Image of X] where a chart would clarify the data. \n\n"

        "### OUTPUT FORMAT (HTML):\n\n"

        "<h2>Compute Market Intelligence</h2>\n"
        "<p>Brief 2-sentence executive summary of the hardware market state (Bullish/Bearish/Constrained).</p>\n\n"

        "\n"
        "<div class='section'>\n"
        "  <div class='news-title'>[1] TOPIC HEADLINE</div>\n"
        "  <p><strong>The News:</strong> What actually happened? Cite sources. <a href='URL'>[1]</a></p>\n"
        "  <p><strong>Impact on Compute:</strong> Technical/Operational impact. (e.g., 'Does this delay H100 shipments?' or 'Does this increase wafer costs?').</p>\n"
        "  <p><strong>Impact on Market:</strong> Financial/Investment impact. (e.g., 'Bullish for equipment makers, Bearish for integrators').</p>\n"
        "</div>\n\n"

        f"RAW INTEL:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html, None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def create_fallback_html(articles, error_msg):
    html = f"<div style='background:#fee;padding:10px;border:1px solid red;'><h3>⚠️ Analysis Failed</h3><pre>{error_msg}</pre></div>"
    for a in articles[:10]:
        html += f"<p><a href='{a['url']}'>{a['title']}</a><br><small>{a['source']['name']}</small></p>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- 3. Sending Email ---")
    
    try:
        with open('email_template.html', 'r', encoding='utf-8') as f:
            template_str = f.read()
    except FileNotFoundError:
        template_str = "<html><body>{{CONTENT}}</body></html>"

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%B %d, %Y'))
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} 🖥️ Compute Intel: {datetime.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(final_body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("vv Email sent successfully!")
    except Exception as e:
        print(f"!! Failed to send email: {e}")

if __name__ == "__main__":
    articles = fetch_compute_news()
    if not articles:
        send_email("<p>No compute market news found today.</p>", "[EMPTY]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[DEBUG]")