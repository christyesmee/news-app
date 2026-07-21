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

# --- SOURCES ---
SOURCES = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "scmp.com", "nikkei.com", "caixinglobal.com", "digitimes.com", 
    "taipeitimes.com", "techcrunch.com", "wired.com", "theregister.com", "anandtech.com"
]

def get_working_model():
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
    
    # We now include the Image URL in the data sent to Gemini
    raw_text = ""
    for i, a in enumerate(articles[:25]):
        safe_title = a['title'].replace('"', "'")
        # Use a placeholder if no image exists
        img_url = a['urlToImage'] if a['urlToImage'] else "NO_IMAGE"
        raw_text += f"ID: {i+1} | Title: {safe_title} | Source: {a['source']['name']} | URL: {a['url']} | IMAGE_URL: {img_url}\n"

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "Role: You are a Compute Market Strategist using 'Smart Brevity' style.\n"
        "Goal: Create a visual, high-impact HTML briefing. Use Emojis to convey sentiment (e.g., 📉 for drops, 🚨 for alerts, 🇨🇳 for China).\n"
        "Constraints: Keep text concise. Maintain strict technical jargon (e.g., 'CoWoS capacity', 'High-NA EUV') but explain impacts simply.\n\n"

        "### INSTRUCTIONS FOR IMAGES & LINKS:\n"
        "1. You MUST pick the best image from the provided 'IMAGE_URL' fields.\n"
        "2. Insert the image at the top of each story block using: <img src='IMAGE_URL' class='story-image'>\n"
        "3. If 'NO_IMAGE' is provided, do not insert an img tag.\n"
        "4. Cite sources as clickable numbers: <a href='URL'>[1]</a>.\n\n"

        "### OUTPUT FORMAT (HTML):\n\n"

        "<h2>⚡ Market Pulse</h2>\n"
        "<p><strong>The Vibe:</strong> (1 sentence summary with an emoji). <strong>The Catalyst:</strong> (1 sentence on the main driver).</p>\n\n"

        "\n"
        "<div class='section'>\n"
        "  \n"
        "  <div class='news-title'>EMOJI + HEADLINE</div>\n"
        "  <p><strong>📰 The Intel:</strong> What happened? (Max 2 sentences). <a href='URL'>[1]</a></p>\n"
        "  <p><strong>💻 Compute Impact:</strong> Technical supply chain effect. Use jargon.</p>\n"
        "  <p><strong>💰 Market Move:</strong> Investment/Price implication.</p>\n"
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
    msg['Subject'] = f"{subject_prefix} ⚡ Compute Intel: {datetime.now().strftime('%Y-%m-%d')}"
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
        send_email("<p>No news found today.</p>", "[EMPTY]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[DEBUG]")