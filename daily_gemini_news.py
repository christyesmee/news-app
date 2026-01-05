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

# Balanced Source List
SOURCES = [
    "reuters.com", "bloomberg.com", "aljazeera.com", "bbc.co.uk", 
    "scmp.com", "dw.com", "cnbc.com", "apnews.com", "economist.com",
    "wsj.com", "ft.com"
]

def get_working_model():
    """Auto-selects the best available Gemini model."""
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        all_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        # Prefer Flash for formatting adherence, Pro for depth.
        # We will try Pro first for the "Detailed Explanation" requirement.
        for m in all_models:
            if "1.5-pro" in m: return m
        for m in all_models:
            if "1.5-flash" in m: return m
        return all_models[0] if all_models else None
    except Exception as e:
        print(f"!! Model selection error: {e}")
        return None

def fetch_global_news():
    print("--- 1. Fetching Global News ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
    
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': '(economy OR politics OR "supply chain" OR geopolitics OR "trade" OR "central bank")',
        'domains': domains,
        'from': date_from,
        'sortBy': 'publishedAt',
        'language': 'en',
        'pageSize': 50, # High volume to find the best 5 stories
        'apiKey': NEWS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name: return None, "No AI models available."

    print(f"--- 2. Analyzing with {model_name} ---")
    
    # Prepare text with IDs for linking
    raw_text = ""
    for i, a in enumerate(articles[:40]):
        safe_title = a['title'].replace('"', "'")
        raw_text += f"ID: {i+1} | Title: {safe_title} | Source: {a['source']['name']} | URL: {a['url']}\n"

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "Role: You are a high-level Strategic Advisor. Your client is an investor building generational wealth.\n"
        "Goal: Distill the news into a 'Billionaire Thinking' brief. Focus on leverage, systems, and implications.\n"
        "Constraints: NO BULLET POINTS. Use paragraphs and clear headers. Maximize readability for mobile.\n\n"

        "### INSTRUCTIONS FOR LINKS:\n"
        "Cite sources using clickable HTML footnotes like: <a href='URL_FROM_INPUT'>[1]</a>.\n\n"

        "### OUTPUT FORMAT (HTML):\n\n"

        "<h2>Billionaire Thinking and Implications</h2>\n"
        "Select exactly **3** Deep Dive Concepts from this week's news. (Quality over Quantity).\n"
        "For each concept, create a <div class='theory-box'>:\n"
        "  <p class='theory-title'>CONCEPT: [Name of Concept]</p>\n"
        "  <p><strong>The Logic:</strong> Provide a detailed, paragraph-length explanation of the economic or strategic theory. Teach the concept thoroughly.</p>\n"
        "  <p><strong>Application & Implication:</strong> Explain how this applies to a specific news event this week (cite it with <a href='URL'>[1]</a>) and what the wealth implications are. How does one profit or protect capital here?</p>\n"
        "</div>\n\n"

        "<h2>Key Political Developments</h2>\n"
        "Identify exactly **5** most critical events. For each use <div class='section'>:\n"
        "  <div class='news-title'>[Concise Headline]</div>\n"
        "  <p><strong>The Event:</strong> A clear paragraph summarizing the facts. No fluff.</p>\n"
        "  <p><strong>The Impact:</strong> Why this changes the global landscape.</p>\n"
        "  <p style='font-size:12px; margin-top:5px;'><em>Source: <a href='URL'>Read Article</a></em></p>\n"
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
    msg['Subject'] = f"{subject_prefix} 🧠 Strategic Brief: {datetime.now().strftime('%Y-%m-%d')}"
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
    articles = fetch_global_news()
    if not articles:
        send_email("<p>No news found today.</p>", "[EMPTY]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[DEBUG]")