import os
import logging
import time
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
    InlineQueryHandler
)
import requests
from aiohttp import web

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('movie_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Bot configuration from environment variables
CONFIG = {
    'token': os.getenv('TELEGRAM_BOT_TOKEN'),
    'admin_ids': [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id],
    'tmdb_api_key': os.getenv('TMDB_API_KEY', '')
}

# MongoDB connection
client = MongoClient(os.getenv('MONGODB_URI'))
db = client[os.getenv('DATABASE_NAME', 'movie_bot')]

# Collections
users_collection = db['users']
searches_collection = db['searches']
favorites_collection = db['favorites']
admins_collection = db['admins']

# Initialize database with admin user if empty
if admins_collection.count_documents({}) == 0 and os.getenv('ADMIN_IDS'):
    for admin_id in CONFIG['admin_ids']:
        admins_collection.update_one(
            {'user_id': admin_id},
            {'$set': {'user_id': admin_id}},
            upsert=True
        )

# Webhook configuration
PORT = int(os.getenv('PORT', 10000))  # Use the PORT environment variable provided by Render
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '') + WEBHOOK_PATH

# ==============================================
# Database Management Functions (MongoDB)
# ==============================================

def add_user(user):
    """Add user to database if not exists"""
    users_collection.update_one(
        {'user_id': user.id},
        {'$set': {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'join_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }},
        upsert=True
    )

def is_admin(user_id):
    """Check if user is admin"""
    return admins_collection.count_documents({'user_id': user_id}) > 0 or user_id in CONFIG['admin_ids']

def log_search(user_id, query, movie_id=None):
    """Log user search in database"""
    searches_collection.insert_one({
        'user_id': user_id,
        'query': query,
        'movie_id': movie_id,
        'search_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

def add_favorite(user_id, movie_id, movie_title):
    """Add movie to user's favorites"""
    # Check if already favorited
    if favorites_collection.count_documents({'user_id': user_id, 'movie_id': movie_id}) > 0:
        return False
    
    favorites_collection.insert_one({
        'user_id': user_id,
        'movie_id': movie_id,
        'movie_title': movie_title,
        'add_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })
    return True

def remove_favorite(user_id, movie_id):
    """Remove movie from user's favorites"""
    result = favorites_collection.delete_one({'user_id': user_id, 'movie_id': movie_id})
    return result.deleted_count > 0

def get_favorites(user_id):
    """Get user's favorite movies"""
    return [(fav['movie_id'], fav['movie_title']) 
            for fav in favorites_collection.find(
                {'user_id': user_id}, 
                {'movie_id': 1, 'movie_title': 1}
            ).sort('add_date', -1)]

def get_user_count():
    """Get total number of users"""
    return users_collection.count_documents({})

def get_search_stats():
    """Get search statistics"""
    # Top searched movies
    top_movies = list(searches_collection.aggregate([
        {'$match': {'movie_id': {'$ne': None}}},
        {'$group': {'_id': '$movie_id', 'count': {'$sum': 1}}},
        {'$sort': {'count': -1}},
        {'$limit': 10}
    ]))
    
    # Total searches
    total_searches = searches_collection.count_documents({})
    
    return top_movies, total_searches

def get_all_users():
    """Get all user IDs for broadcasting"""
    return [user['user_id'] for user in users_collection.find({}, {'user_id': 1})]

# ==============================================
# TMDB API Functions
# ==============================================

def retry_on_failure(max_retries=3, delay=5):
    """Decorator to retry a function when it fails"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    logger.warning(f"Attempt {retries}/{max_retries} failed for {func.__name__}: {str(e)}")
                    if retries < max_retries:
                        time.sleep(delay)
            else:
                logger.error(f"Max retries reached for {func.__name__}")
                raise
        return wrapper
    return decorator

@retry_on_failure()
async def make_tmdb_request(url):
    """Make a request to TMDB API with error handling"""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"TMDB API request failed: {str(e)}")
        return None

async def get_movie_by_name(movie_name, year=None):
    """Search for a movie by name and optional year"""
    logger.info(f"Searching for movie: {movie_name} (Year: {year if year else 'N/A'})")
    
    url = f"https://api.themoviedb.org/3/search/movie?api_key={CONFIG['tmdb_api_key']}&query={movie_name}"
    if year:
        url += f"&year={year}"
    
    response = await make_tmdb_request(url)
    if not response:
        logger.error("Failed to get response from TMDB API")
        return None
    
    logger.debug(f"TMDB Search Response: {response}")
    
    if response.get("results"):
        movie_id = response["results"][0].get("id")
        if movie_id:
            return await get_movie_by_id(movie_id)
        logger.error("No ID found in TMDB response results")
    else:
        logger.warning(f"No results found for movie: {movie_name}")
    return None

async def get_movie_by_id(movie_id):
    """Get detailed information about a movie by TMDB ID"""
    logger.info(f"Fetching details for movie ID: {movie_id}")
    
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={CONFIG['tmdb_api_key']}&append_to_response=videos,recommendations"
    response = await make_tmdb_request(url)
    if not response:
        logger.error("Failed to get movie details from TMDB API")
        return None
    
    logger.debug(f"TMDB Movie Details Response: {response}")
    
    if "id" not in response:
        logger.error(f"Invalid movie ID or no details found for ID: {movie_id}")
        return None
    
    try:
        movie_data = {
            "id": response["id"],
            "title": response.get("title", "N/A"),
            "year": response.get("release_date", "N/A")[:4] if response.get("release_date") else "N/A",
            "runtime": f"{response.get('runtime', 'N/A')} min" if response.get("runtime") else "N/A",
            "genres": ", ".join([genre["name"] for genre in response.get("genres", [])]) or "N/A",
            "language": response.get("original_language", "N/A").upper(),
            "rating": str(round(response.get("vote_average", 0), 1)) if response.get("vote_average") else "N/A",
            "overview": response.get("overview", "No overview available."),
            "poster_url": f"https://image.tmdb.org/t/p/original{response['poster_path']}" if response.get("poster_path") else None,
            "trailer_url": next((f"https://www.youtube.com/watch?v={video['key']}" 
                                for video in response.get("videos", {}).get("results", []) 
                                if video["type"] == "Trailer" and video["site"] == "YouTube"), None),
            "tmdb_link": f"https://www.themoviedb.org/movie/{movie_id}",
            "recommendations": get_recommendations(response.get("recommendations", {}).get("results", [])[:5])
        }
        logger.info(f"Successfully fetched details for: {movie_data['title']}")
        return movie_data
    except Exception as e:
        logger.error(f"Error processing movie details: {str(e)}")
        return None

def get_recommendations(recommendations):
    """Format recommendations list"""
    return [
        {
            "id": movie.get("id"),
            "title": movie.get("title"),
            "year": movie.get("release_date", "")[:4] if movie.get("release_date") else "N/A",
            "poster": f"https://image.tmdb.org/t/p/w200{movie['poster_path']}" if movie.get("poster_path") else None
        }
        for movie in recommendations
    ]

async def get_trending_movies():
    """Get currently trending movies"""
    url = f"https://api.themoviedb.org/3/trending/movie/week?api_key={CONFIG['tmdb_api_key']}"
    response = await make_tmdb_request(url)
    if not response or not response.get("results"):
        return None
    
    return [await get_movie_by_id(movie["id"]) for movie in response.get("results", [])[:5]]

async def get_popular_movies():
    """Get popular movies"""
    url = f"https://api.themoviedb.org/3/movie/popular?api_key={CONFIG['tmdb_api_key']}"
    response = await make_tmdb_request(url)
    if not response or not response.get("results"):
        return None
    
    return [await get_movie_by_id(movie["id"]) for movie in response.get("results", [])[:5]]

def format_movie_message(movie, include_recommendations=True):
    """Format movie details into a nicely structured message"""
    try:
        text = (
            f"🎬 *{movie['title']}* ({movie['year']})\n"
            f"⭐ Rᴀᴛɪɴɢ: {movie['rating']}/10\n"
            f"⏳ Rᴜɴᴛɪᴍᴇ: {movie['runtime']}\n"
            f"📌 Gᴇɴʀᴇꜱ: {movie['genres']}\n"
            f"🌍 Lᴀɴɢᴜᴀɢᴇ: {movie['language']}\n\n"
            f"📖 *Oᴠᴇʀᴠɪᴇᴡ:*\n{movie['overview']}\n\n"
            f"🔗 [More Info on TMDB]({movie['tmdb_link']})"
        )

        if movie["trailer_url"]:
            text += f"\n🎥 [Watch Trailer]({movie['trailer_url']})"

        # Add recommendations if available
        if include_recommendations and movie.get("recommendations"):
            text += "\n\n🎥 *ﮩﮩ٨ـﮩﮩYᴏᴜ Mɪɢʜᴛ Aʟꜱᴏ Lɪᴋᴇﮩﮩـ٨ﮩ:*"
            for rec in movie["recommendations"]:
                text += f"\n𒆜 [{rec['title']} ({rec['year']})](https://www.themoviedb.org/movie/{rec['id']})"

        return text
    except Exception as e:
        logger.error(f"Error formatting movie message: {str(e)}")
        return "Error formatting movie information."

def format_movie_list(movies, title):
    """Format a list of movies"""
    text = f"*{title}*\n\n"
    for movie in movies:
        if movie:
            text += f"🎬 [{movie['title']} ({movie['year']})](https://www.themoviedb.org/movie/{movie['id']})\n"
            text += f"⭐ {movie['rating']}/10 | ⏳ {movie['runtime']}\n\n"
    return text

# ==============================================
# Telegram Bot Command Handlers
# ==============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message and instructions"""
    add_user(update.effective_user)
    
    help_text = (
        " ミ★ 𝐓𝐌𝐃𝐁 𝐁𝐨𝐭 𝐇𝐞𝐥𝐩 ★彡\n\n"
        "I Cᴀɴ Fᴇᴛᴄʜ Mᴏᴠɪᴇ Dᴇᴛᴀɪʟꜱ Fʀᴏᴍ *Tᴍᴅʙ Wᴇʙꜱɪᴛᴇ* Aɴᴅ Mᴏʀᴇ!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 𝐒𝐞𝐚𝐫𝐜𝐡 𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:\n"
        "`/search <movie name> [year]` - Sᴇᴀʀʜ Bʏ Nᴀᴍᴇ\n"
        "`/id <tmdb_id>` - Sᴇᴀʀᴄʜ Bʏ Tᴍᴅʙ Iᴅ\n"
        "`/trending` - Cᴜʀʀᴇɴᴛʟʏ Tʀᴇɴᴅɪɴɢ Mᴏᴠɪᴇꜱ\n"
        "`/popular` - Mᴏꜱᴛ Pᴏᴘᴜʟᴀʀ Mᴏᴠɪᴇꜱ\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💖 𝐅𝐚𝐯𝐨𝐫𝐢𝐭𝐞 𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:\n"
        "`/favorites` - Vɪᴇᴡ Yᴏᴜʀ Sᴀᴠᴇᴅ Mᴏᴠɪᴇꜱ\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "𒆜 𝐒𝐞𝐚𝐫𝐜𝐡 𝐰𝐢𝐭𝐡 𝐢𝐧𝐥𝐢𝐧𝐞:\n"
        "*Exᴀᴍᴘʟᴇ*: `@Themoviedatabasee_bot <Movie Name>`\n"
        "Aꜰᴛᴇʀ ɢᴇᴛᴛɪɴɢ ᴛʜᴇ ᴍᴏᴠɪᴇ ɪᴅ ꜰʀᴏᴍ "
        "Tʜᴇ Iɴʟɪɴᴇ Sᴇᴀʀᴄʜ *Cᴏᴘʏ Iᴛ* Aɴᴅ *Sᴇᴀʀᴄʜ* Wɪᴛʜ Tʜᴇ Hᴇʟᴘ Oꜰ Tʜᴇ `/id` Cᴏᴍᴍᴀɴᴅ\n\n"
        "●━━━━━━━━━━━━━━━━━━━━●"
    )
    
    # Create inline keyboard with join buttons
    keyboard = [
        [InlineKeyboardButton("📢 Jᴏɪɴ Mᴀɪɴ Cʜᴀɴɴᴇʟ", url="https://t.me/Freenethubz")],
        [InlineKeyboardButton("📢 Jᴏɪɴ Bᴀᴄᴋᴜᴘ Cʜᴀɴɴᴇʟ", url="https://t.me/Freenethubchannel")],
        [InlineKeyboardButton("📢 Jᴏɪɴ Bᴏᴛ Hᴇʟᴘ", url="https://t.me/Megahubbots")],
        [InlineKeyboardButton("📢 Jᴏɪɴ Wʜᴀꜱᴛᴀᴘᴘ Cʜᴀɴɴᴇʟ", url="https://whatsapp.com/channel/0029VaDnY2y0rGiPV41aSX0l")],
        [InlineKeyboardButton("📢 Sᴜʙꜱᴄʀɪʙᴇ Oᴜʀ Yᴏᴜᴛᴜʙᴇ", url="https://youtube.com/@freenethubtech?si=82p5899ranDoE-hB")]
    ]
    
    await update.message.reply_text(
        help_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def contact_us(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send contact information with working buttons"""
    contact_text = (
        "📞 ★彡( 𝕮𝖔𝖓𝖙𝖆𝖈𝖙 𝖀𝖘 )彡★ 📞\n\n"
        "📧 Eᴍᴀɪʟ: `freenethubbusiness@gmail.com`\n\n"
        "Fᴏʀ Aɴʏ Iꜱꜱᴜᴇꜱ, Bᴜꜱɪɴᴇꜱꜱ Dᴇᴀʟꜱ Oʀ IɴQᴜɪʀɪᴇꜱ, Pʟᴇᴀꜱᴇ Rᴇᴀᴄʜ Oᴜᴛ Tᴏ Uꜱ \n\n"
        "❗ *ONLY FOR BUSINESS AND HELP, DON'T SPAM!*"
    )
    
    # Create inline keyboard with info buttons
    keyboard = [[InlineKeyboardButton("📩 Mᴇꜱꜱᴀɢᴇ Aᴅᴍɪɴ", url="https://t.me/Silando")]]
    
    await update.message.reply_text(
        contact_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def search_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie search by name"""
    try:
        add_user(update.effective_user)
        query = update.message.text.replace("/search", "").strip()
        logger.info(f"Received search query: {query} from user {update.effective_user.id}")
        
        if not query:
            await update.message.reply_text(
                "Pʟᴇᴀꜱᴇ Pʀᴏᴠɪᴅᴇ ᴀ Mᴏᴠɪᴇ Nᴀᴍᴇ. Exᴀᴍᴘʟᴇ:\n`/search Avatar 2009`",
                parse_mode="Markdown"
            )
            return

        # Split into movie name and optional year
        parts = query.rsplit(" ", 1)
        movie_name = parts[0]
        year = parts[1] if len(parts) > 1 and parts[1].isdigit() else None

        movie = await get_movie_by_name(movie_name, year)
        if movie:
            log_search(update.effective_user.id, query, movie["id"])
        await send_movie_response(update, movie)
        
    except Exception as e:
        logger.error(f"Error in search_movie: {str(e)}")
        await update.message.reply_text("❌ Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ Wʜɪʟᴇ Pʀᴏᴄᴇꜱꜱɪɴɢ Yᴏᴜʀ RᴇQᴜᴇꜱᴛ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def search_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie search by TMDB ID"""
    try:
        add_user(update.effective_user)
        movie_id = update.message.text.replace("/id", "").strip()
        logger.info(f"Received ID search: {movie_id} from user {update.effective_user.id}")
        
        if not movie_id or not movie_id.isdigit():
            await update.message.reply_text(
                "Pʟᴇᴀꜱᴇ Pʀᴏᴠɪᴅᴇ ᴀ Vᴀʟɪᴅ Tᴍᴅʙ Iᴅ. Exᴀᴍᴘʟᴇ:\n`/id 27205`",
                parse_mode="Markdown"
            )
            return

        movie = await get_movie_by_id(movie_id)
        if movie:
            log_search(update.effective_user.id, f"ID:{movie_id}", movie["id"])
        await send_movie_response(update, movie)
        
    except Exception as e:
        logger.error(f"Error in search_by_id: {str(e)}")
        await update.message.reply_text("❌  Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ Wʜɪʟᴇ Pʀᴏᴄᴇꜱꜱɪɴɢ Yᴏᴜʀ RᴇQᴜᴇꜱᴛ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trending movies"""
    try:
        add_user(update.effective_user)
        movies = await get_trending_movies()
        if movies:
            text = format_movie_list(movies, " ❝🔥 𝐂𝐮𝐫𝐫𝐞𝐧𝐭𝐥𝐲 𝐓𝐫𝐞𝐧𝐝𝐢𝐧𝐠 𝐌𝐨𝐯𝐢𝐞𝐬❞")
            await update.message.reply_text(text, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Cᴏᴜʟᴅ Nᴏᴛ Fᴇᴛᴄʜ Tʀᴇɴᴅɪɴɢ Mᴏᴠɪᴇꜱ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ Lᴀᴛᴇʀ.")
    except Exception as e:
        logger.error(f"Error in show_trending: {str(e)}")
        await update.message.reply_text("❌ Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def show_popular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show popular movies"""
    try:
        add_user(update.effective_user)
        movies = await get_popular_movies()
        if movies:
            text = format_movie_list(movies, "🌟 *❝𝐌𝐨𝐬𝐭 𝐏𝐨𝐩𝐮𝐥𝐚𝐫 𝐌𝐨𝐯𝐢𝐞𝐬❞*")
            await update.message.reply_text(text, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Cᴏᴜʟᴅ Nᴏᴛ Fᴇᴛᴄʜ Pᴏᴘᴜʟᴀʀ Mᴏᴠɪᴇꜱ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ Lᴀᴛᴇʀ.")
    except Exception as e:
        logger.error(f"Error in show_popular: {str(e)}")
        await update.message.reply_text("❌ Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's favorite movies with inline buttons to view them"""
    try:
        add_user(update.effective_user)
        favorites = get_favorites(update.effective_user.id)
        
        if not favorites:
            await update.message.reply_text("Yᴏᴜ Hᴀᴠᴇɴ'ᴛ Sᴀᴠᴇᴅ Aɴʏ Fᴀᴠᴏʀɪᴛᴇꜱ Yᴇᴛ. Uꜱᴇ Tʜᴇ ❤️ Bᴜᴛᴛᴏɴ Aꜰᴛᴇʀ Sᴇᴀʀᴄʜɪɴɢ Fᴏʀ Mᴏᴠɪᴇꜱ Tᴏ Sᴀᴠᴇ Tʜᴇᴍ.")
            return
            
        text = "⭐ Yᴏᴜʀ Fᴀᴠᴏʀɪᴛᴇ Mᴏᴠɪᴇꜱ:\n\n"
        keyboard = []
        
        for movie_id, title in favorites[:10]:  # Show first 10 favorites
            keyboard.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"view_{movie_id}")])
        
        if len(favorites) > 10:
            text += f"Sʜᴏᴡɪɴɢ 10 Oꜰ {len(favorites)} Fᴀᴠᴏʀɪᴛᴇꜱ\n"
        
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in show_favorites: {str(e)}")
        await update.message.reply_text("❌ Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ Wʜɪʟᴇ Fᴇᴛᴄʜɪɴɢ Yᴏᴜʀ Fᴀᴠᴏʀɪᴛᴇꜱ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def handle_view_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle viewing a favorite movie"""
    query = update.callback_query
    await query.answer()
    
    try:
        movie_id = query.data.split('_')[1]
        movie = await get_movie_by_id(movie_id)
        
        if not movie:
            await query.answer("Mᴏᴠɪᴇ Nᴏᴛ Fᴏᴜɴᴅ!")
            return
            
        # Send the movie details with from_favorites=True
        await send_movie_response(query, movie, from_favorites=True)
        
    except Exception as e:
        logger.error(f"Error in handle_view_favorite: {str(e)}")
        await query.answer("❌ Error loading movie")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot usage statistics (admin only)"""
    try:
        add_user(update.effective_user)
        
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ This command is for admins only.")
            return
            
        user_count = get_user_count()
        top_movies, total_searches = get_search_stats()
        
        text = f"📊 *Bᴏᴛ Sᴛᴀᴛɪꜱᴛɪᴄꜱ*\n\n"
        text += f"👥 Tᴏᴛᴀʟ Uꜱᴇʀꜱ: {user_count}\n"
        text += f"🔍 Tᴏᴛᴀʟ Sᴇᴀʀᴄʜᴇꜱ: {total_searches}\n\n"
        text += "🎥 *❝𝐓𝐨𝐩 𝟏𝟎 𝐌𝐨𝐬𝐭 𝐒𝐞𝐚𝐫𝐜𝐡𝐞𝐝 𝐌𝐨𝐯𝐢𝐞𝐬❞:*\n"
        
        for movie in top_movies:
            movie_details = await get_movie_by_id(movie["_id"])
            if movie_details:
                text += f"- {movie_details['title']}: {movie['count']} Sᴇᴀʀᴄʜᴇꜱ\n"
            else:
                text += f"- ID {movie['_id']}: {movie['count']} Sᴇᴀʀᴄʜᴇꜱ\n"
                
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in show_stats: {str(e)}")
        await update.message.reply_text("❌ Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ Wʜɪʟᴇ Fᴇᴛᴄʜɪɴɢ Sᴛᴀᴛɪꜱᴛɪᴄꜱ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (admin only)"""
    try:
        add_user(update.effective_user)
        
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Tʜɪꜱ Cᴏᴍᴍᴀɴᴅ Iꜱ Fᴏʀ Aᴅᴍɪɴꜱ Oɴʟʏ. Pʟᴇᴀꜱᴇ Dᴏɴ'ᴛ Cʀʏ .")
            return
            
        broadcast_text = update.message.text.replace("/broadcast", "").strip()
        if not broadcast_text:
            await update.message.reply_text("Pʟᴇᴀꜱᴇ Pʀᴏᴠɪᴅᴇ A Mᴇꜱꜱᴀɢᴇ Tᴏ Bʀᴏᴀᴅᴄᴀꜱᴛ. Exᴀᴍᴘʟᴇ:\n`/broadcast Hello users!`")
            return
            
        users = get_all_users()
        success = 0
        failures = 0
        
        await update.message.reply_text(f"📢 Sᴛᴀʀᴛɪɴɢ Bʀᴏᴀᴅᴄᴀꜱᴛ Tᴏ {len(users)} users...")
        
        for user_id in users:
            try:
                await context.bot.send_message(
                    user_id,
                    f"📢 *ᕚ(𝐀𝐧𝐧𝐨𝐮𝐧𝐜𝐞𝐦𝐞𝐧𝐭 𝐟𝐫𝐨𝐦 𝐚𝐝𝐦𝐢𝐧)ᕘ:*\n\n{broadcast_text}",
                    parse_mode="Markdown"
                )
                success += 1
                time.sleep(0.1)  # Rate limiting
            except Exception as e:
                logger.warning(f"Fᴀɪʟᴇᴅ Tᴏ Sᴇɴᴅ Bʀᴏᴀᴅᴄᴀꜱᴛ Tᴏ Uꜱᴇʀ {user_id}: {str(e)}")
                failures += 1
                
        await update.message.reply_text(f"📢 Bʀᴏᴀᴅᴄᴀꜱᴛ Cᴏᴍᴘʟᴇᴛᴇᴅ!\n✅ Sᴜᴄᴄᴇꜱꜱ: {success}\n❌ Fᴀɪʟᴜʀᴇꜱ: {failures}")
    except Exception as e:
        logger.error(f"Error in broadcast_message: {str(e)}")
        await update.message.reply_text("❌ Aɴ Eʀʀᴏʀ Oᴄᴄᴜʀʀᴇᴅ Dᴜʀɪɴɢ Bʀᴏᴀᴅᴄᴀꜱᴛ. Pʟᴇᴀꜱᴇ Tʀʏ Aɢᴀɪɴ.")

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline movie searches"""
    try:
        query = update.inline_query.query.strip()
        if not query:
            return
            
        url = f"https://api.themoviedb.org/3/search/movie?api_key={CONFIG['tmdb_api_key']}&query={query}"
        response = await make_tmdb_request(url)
        
        if not response or not response.get("results"):
            return
            
        results = []
        for movie in response.get("results", [])[:5]:  # Limit to 5 results
            if not movie.get("id"):
                continue
                
            # Get basic details without full API call
            title = movie.get("title", "N/A")
            year = movie.get("release_date", "")[:4] if movie.get("release_date") else "N/A"
            overview = movie.get("overview", "No overview available")
            
            # Format the result
            text = (
                f"🎬 *{title}* ({year})\n\n"
                f"📖 {overview[:200]}...\n\n"
                f"🔍 Use `/id {movie['id']}` for full details"
            )
            
            result = {
                "type": "article",
                "id": str(movie["id"]),
                "title": f"{title} ({year})",
                "description": overview[:100] + "..." if overview else "No overview",
                "input_message_content": {
                    "message_text": text,
                    "parse_mode": "Markdown"
                }
            }
            results.append(result)
            
        await update.inline_query.answer(results)
    except Exception as e:
        logger.error(f"Error in handle_inline_query: {str(e)}")

async def send_movie_response(update, movie, from_favorites=False):
    """Send the formatted movie response to the user"""
    if movie:
        text = format_movie_message(movie)
        
        # Create inline keyboard
        keyboard = []
        
        if from_favorites:
            remove_btn = InlineKeyboardButton(
                text="❌ Rᴇᴍᴏᴠᴇ Fʀᴏᴍ Fᴀᴠᴏʀɪᴛᴇꜱ",
                callback_data=f"remove_{movie['id']}"
            )
            keyboard.append([remove_btn])
        else:
            add_btn = InlineKeyboardButton(
                text="❤️ Sᴀᴠᴇ Tᴏ Fᴀᴠᴏʀɪᴛᴇꜱ",
                callback_data=f"fav_{movie['id']}"
            )
            keyboard.append([add_btn])
        
        # Add join buttons
        keyboard.append([InlineKeyboardButton("📢 Jᴏɪɴ Mᴀɪɴ Cʜᴀɴɴᴇʟ", url="https://t.me/Freenethubz")])
        keyboard.append([InlineKeyboardButton("📢 Cʀᴇᴀᴛᴏʀ Cʜᴀɴɴᴇʟ", url="https://t.me/Megahubbots")])
        
        # Send movie details with poster if available
        if movie.get("poster_url"):
            try:
                await update.message.reply_photo(
                    photo=movie["poster_url"],
                    caption=text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            except Exception as e:
                logger.warning(f"Failed to send photo, falling back to text: {str(e)}")
        
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text("❌ Mᴏᴠɪᴇ Nᴏᴛ Fᴏᴜɴᴅ. Pʟᴇᴀꜱᴇ Cʜᴇᴄᴋ Tʜᴇ Nᴀᴍᴇ Oʀ Iᴅ Aɴᴅ Tʀʏ Aɢᴀɪɴ.")

async def handle_favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle favorite button clicks"""
    query = update.callback_query
    
    try:
        movie_id = query.data.split('_')[1]
        movie = await get_movie_by_id(movie_id)
        
        if not movie:
            await query.answer("Movie Not Found!", show_alert=True)
            return
            
        success = add_favorite(query.from_user.id, movie_id, movie["title"])
        if success:
            await query.answer(f"❤️ {movie['title']} added to favorites!", show_alert=True)
        else:
            await query.answer(f"❤️ {movie['title']} is already in favorites!", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error in handle_favorite_callback: {str(e)}")
        await query.answer("❌ Error saving to favorites", show_alert=True)

async def handle_remove_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle remove from favorites button clicks"""
    query = update.callback_query
    
    try:
        movie_id = query.data.split('_')[1]
        movie = await get_movie_by_id(movie_id)
        
        if not movie:
            await query.answer("Movie Not Found!", show_alert=True)
            return
            
        removed = remove_favorite(query.from_user.id, movie_id)
        if removed:
            # Create the new keyboard
            keyboard = [
                [InlineKeyboardButton("❤️ Save To Favorites", callback_data=f"fav_{movie['id']}")],
                [InlineKeyboardButton("📢 Join Main Channel", url="https://t.me/Freenethubz")],
                [InlineKeyboardButton("📢 Creator Channel", url="https://t.me/Megahubbots")]
            ]
            
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            await query.answer(f"❌ {movie['title']} removed from favorites!", show_alert=True)
        else:
            await query.answer(f"{movie['title']} wasn't in your favorites!", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error in handle_remove_favorite: {str(e)}")
        await query.answer("❌ Error removing from favorites", show_alert=True)

# Add handlers to the application
def main():
    """Run the bot"""
    application = Application.builder().token(CONFIG['token']).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("contactus", contact_us))
    application.add_handler(CommandHandler("search", search_movie))
    application.add_handler(CommandHandler("id", search_by_id))
    application.add_handler(CommandHandler("trending", show_trending))
    application.add_handler(CommandHandler("popular", show_popular))
    application.add_handler(CommandHandler("favorites", show_favorites))
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(CommandHandler("broadcast", broadcast_message))
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(handle_favorite_callback, pattern="^fav_"))
    application.add_handler(CallbackQueryHandler(handle_remove_favorite, pattern="^remove_"))
    application.add_handler(CallbackQueryHandler(handle_view_favorite, pattern="^view_"))
    
    # Inline query handler
    application.add_handler(InlineQueryHandler(handle_inline_query))
    
    # Start the bot with webhook if running on Render
    if os.getenv('RENDER'):
        application.run_webhook(
            listen="0.0.0.0",  # Listen on all interfaces
            port=PORT,         # Bind to the PORT environment variable
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL
        )
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
