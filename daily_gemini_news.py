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
    "scmp.com", "dw.com", "cnbc.com", "apnews.com", "economist.com"
]

def get_working_model():
    """Auto-selects the best available Gemini model."""
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        all_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        # Prefer Flash for this task as it follows formatting instructions very well
        for m in all_models:
            if "1.5-flash" in m: return m
        for m in all_models:
            if "1.5-pro" in m: return m
        return all_models[0] if all_models else None
    except Exception as e:
        print(f"!! Model selection error: {e}")
        return None

def fetch_global_news():
    print("--- 1. Fetching Global News ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')  # 7 days for richer weekly insights
    
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': '(economy OR politics OR inflation OR tariffs OR "oil prices" OR "central bank" OR "interest rate" OR geopolitics OR "trade war" OR "supply chain" OR "fiscal policy") '
             'AND (global OR international OR world OR US OR China OR Europe OR "Latin America" OR "Middle East")',
        'domains': domains,
        'from': date_from,
        'sortBy': 'publishedAt',
        'language': 'en',
        'pageSize': 100,  # Max allowed — more diverse, high-quality input for Gemini
        'apiKey': NEWS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()  # Raises HTTPError for bad responses (4xx/5xx)
        
        data = response.json()
        articles = data.get("articles", [])
        total_results = data.get("totalResults", 0)
        
        print(f"-> Retrieved {len(articles)} articles (out of ~{total_results} total matches).")
        return articles
        
    except requests.exceptions.Timeout:
        print("!! News API request timed out.")
        return []
    except requests.exceptions.HTTPError as e:
        print(f"!! News API HTTP error: {e} (Status: {e.response.status_code})")
        return []
    except requests.exceptions.RequestException as e:
        print(f"!! News API request failed: {e}")
        return []
    except ValueError:  # JSON decode error
        print("!! Invalid JSON response from News API.")
        return []
    except Exception as e:
        print(f"!! Unexpected error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name: return None, "No AI models available."

    print(f"--- 2. Analyzing with {model_name} ---")
    
    # Prepare text with URLs for the AI to link
    raw_text = ""
    for i, a in enumerate(articles[:20]):
        safe_title = a['title'].replace('"', "'")
        raw_text += f"ID: {i+1} | Title: {safe_title} | Source: {a['source']['name']} | URL: {a['url']}\n"

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
    "Role: You are a world-class strategic mentor teaching an intelligent aspiring billionaire how to think like the top 10 billionaires "
    "(Buffett, Musk, Bezos, Gates, Thiel, Zuckerberg, etc.). Your student is new to global macro but highly motivated. "
    "Goal: First teach billionaire-level thinking principles applied to this week's news, then present the key events in clear, actionable boxes. "
    "Keep the entire report concise (max 10-minute read) and highly educational.\n\n"

    "### STRICT RULES:\n"
    "- Use ONLY information from the provided articles. Do not invent facts, events, or numbers.\n"
    "- When citing sources, make them clickable HTML links using the exact URL from the raw intel.\n"
    "- Example: <a href='https://example.com/article'>Reuters - Jan 05, 2026</a>\n"
    "- Remain balanced and objective.\n"
    "- Use provided CSS classes only (.section, .news-title, .theory-box, .theory-title).\n\n"

    "### OUTPUT FORMAT (Clean HTML - Start directly with content):\n\n"

    "<h3>Deep Dive: Billionaire Thinking & Implications</h3>\n"
    "<p>Teach 4–5 core principles from books and advice recommended by the world's top billionaires "
    "(e.g., Buffett: The Intelligent Investor; Musk/Thiel: Zero to One; Gates/Zuckerberg: Sapiens; Bezos/Grove: High Output Management). "
    "Apply each directly to this week's news for wealth-building insight.</p>\n"
    "<ul>\n"
    "  <li><strong>Principle & Book:</strong> Brief explanation of the core idea/logic.</li>\n"
    "  <li><strong>Application to This Week's News:</strong> How the principle connects to current events.</li>\n"
    "  <li><strong>Implications:</strong> For your personal wealth journey • For the world</li>\n"
    "</ul>\n\n"

    "<h3>Key Political Developments</h3>\n"
    "<p>5–8 most significant events this week. For each, create a visual box using <div class='section'>:</p>\n"
    "<div class='section'>\n"
    "  <p class='news-title'>Short, powerful event summary</p>\n"
    "  <p><strong>Link:</strong> <a href='URL_FROM_ARTICLE'>Source Name - Date</a></p>\n"
    "  <p><strong>Implications:</strong> For you (wealth opportunities/risks) • For the world</p>\n"
    "</div>\n\n"

    "<h3>Transparent Sources (Top 30 Articles Used)</h3>\n"
    "<ul>\n"
    "  <li><a href='URL'>Article Title — Source (Date)</a></li>\n"
    "  ... (list provided by code, but include placeholder style)\n"
    "</ul>\n\n"

    "Tone: Professional, educational, motivational. Encourage long-term thinking and disciplined action. "
    "Designed for delivery every 2–3 days to build compounding knowledge.\n\n"

    f"RAW INTEL (use these for all facts, links, and applications):\n{raw_text}"
)

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html, None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def create_fallback_html(articles, error_msg):
    html = f"<div style='background:#fee;padding:10px;border:1px solid red;'><h3>⚠️ Analysis Failed</h3><pre>{error_msg}</pre></div><ul>"
    for a in articles[:10]:
        html += f"<li><a href='{a['url']}'>{a['title']}</a> - {a['source']['name']}</li>"
    html += "</ul>"
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
    msg['Subject'] = f"{subject_prefix} 🎓 Daily Mentor: {datetime.now().strftime('%Y-%m-%d')}"
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