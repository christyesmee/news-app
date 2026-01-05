import os
import requests
import smtplib
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

MODEL_NAME = "gemini-1.5-pro"

def fetch_global_news():
    print("--- 1. Fetching News ---")
    
    # SIMPLIFIED QUERY: Complex queries often fail on free plans. 
    # We switch to a broader query to GUARANTEE results.
    url = (
        f"https://newsapi.org/v2/top-headlines?"
        f"category=general&"
        f"language=en&"
        f"pageSize=10&"
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Status: {data.get('status')}")
        print(f"-> Found {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    print(f"--- 2. Analyzing with {MODEL_NAME} ---")
    if not articles:
        return None

    # Prepare data
    raw_text = ""
    for i, a in enumerate(articles):
        raw_text += f"{i+1}. {a['title']} (Source: {a['source']['name']})\n"

    print(f"-> Input Tokens (Approx): {len(raw_text)}")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)

    # BLOCK_NONE is critical for news
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    prompt = (
        "Summarize these news headlines into a daily briefing HTML format. "
        "Use <div class='section'> for each topic. "
        "Include a <div class='theory-box'> explaining the context. "
        "If the news is boring, explain why it matters.\n\n"
        f"HEADLINES:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        print("-> Gemini Response Received")
        
        if not response.parts:
            print("!! Gemini returned EMPTY response (Safety Blocked).")
            return None
            
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html
    except Exception as e:
        print(f"!! Gemini Error: {e}")
        return None

def create_fallback_html(articles):
    """Creates a simple HTML list if AI fails"""
    print("--- Generating Fallback HTML ---")
    html = "<h3>⚠️ AI Generation Failed - Here are the Raw Headlines</h3><ul>"
    for a in articles:
        html += f"<li><b>{a['title']}</b><br><i>{a['source']['name']}</i></li><br>"
    html += "</ul>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- 3. Sending Email ---")
    
    # Load Template or Default
    try:
        with open('email_template.html', 'r') as f:
            template_str = f.read()
    except:
        template_str = "<html><body><h1>Daily Brief</h1>{{CONTENT}}</body></html>"

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%Y-%m-%d'))
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} 🌍 World Brief: {datetime.now().strftime('%Y-%m-%d')}"
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
    print("=== STARTING JOB ===")
    articles = fetch_global_news()
    
    if not articles:
        print("!! No articles found. Sending Error Email.")
        send_email("<p>Error: NewsAPI returned 0 articles today. Check API Key or Query.</p>", "[ERROR]")
    else:
        analysis_html = analyze_news(articles)
        
        if analysis_html:
            send_email(analysis_html)
        else:
            print("!! AI Output was empty. Sending Fallback.")
            fallback = create_fallback_html(articles)
            send_email(fallback, "[FALLBACK]")