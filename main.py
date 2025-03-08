import asyncio
import logging
import requests
from datetime import datetime, time
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from sqlalchemy import Column, String, Time, DateTime, Integer, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Константы
API_KEY = "mmldJYh2h33pboUakTFsohCOa1VLR5KCP4OBW0j5+y0="
TOKEN = '7279266289:AAEZhEkpNREbkFUp6DELAlWoKXEjFvc8x4Y'
CHECK_INTERVAL = 60
PRODUCT_IDS = set()

# Инициализация бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальная переменная для хранения chat_id
CHAT_ID = None

Base = declarative_base()


class UserConfig(Base):
    """
    Модель для хранения настроек каждого пользователя:
    - chat_id: уникальный идентификатор чата Telegram
    - api_key: API-ключ для доступа к внешнему API
    - report_time: время отправки ежедневного отчёта
    - updated_at: время последнего обновления записи
    """
    __tablename__ = 'user_config'

    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), unique=True, nullable=False)
    api_key = Column(String(255), nullable=False)
    report_time = Column(Time, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<UserConfig(chat_id='{self.chat_id}', api_key='{self.api_key}', report_time='{self.report_time}')>"


# Настройка подключения к БД (здесь используется SQLite)
engine = create_engine('sqlite:///bot_config.db', echo=False)
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


def get_session():
    """Возвращает сессию для работы с БД."""
    return SessionLocal()


def get_user_config(chat_id: str):
    """Извлекает конфигурацию пользователя по chat_id."""
    session = get_session()
    config = session.query(UserConfig).filter(UserConfig.chat_id == chat_id).first()
    session.close()
    return config


def save_user_config(chat_id: str, api_key: str, report_time: time):
    """Создает или обновляет конфигурацию пользователя в БД."""
    session = get_session()
    config = session.query(UserConfig).filter(UserConfig.chat_id == chat_id).first()
    if config:
        config.api_key = api_key
        config.report_time = report_time
    else:
        config = UserConfig(chat_id=chat_id, api_key=api_key, report_time=report_time)
        session.add(config)
    session.commit()
    session.close()


# ---------------------------- FSM: Сбор настроек от пользователя ----------------------------

class ConfigStates(StatesGroup):
    waiting_for_api_key = State()
    waiting_for_report_time = State()


# ---------------------------- Функции работы с API и форматирования сообщений ----------------------------

def get_orders(api_key: str):
    """
    Получает заказы через внешний API.
    Параметры запроса и URL задаются статически (пример для демонстрации).
    """
    url = "https://api-seller.uzum.uz/api/seller-openapi/v1/fbs/orders"
    params = {"shopIds": "60348", "status": "PACKING"}
    headers = {"Authorization": f"{api_key}"}

    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе к API: {e}")
        return None


def format_order_message(order):
    """
    Формирует текстовое сообщение для уведомления о заказе.
    Также возвращает список валидных URL изображений и подробную информацию по товарам.
    """
    order_id = order.get('id', 'Нет данных')
    deliver_until = order.get('deliverUntil', '')
    formatted_date = 'Не указано'

    if deliver_until:
        if isinstance(deliver_until, str):
            try:
                deliver_date = datetime.fromisoformat(deliver_until.replace('Z', '+00:00'))
                formatted_date = deliver_date.strftime('%d.%m.%Y')
            except ValueError:
                logger.error(f"Ошибка при обработке даты {deliver_until}")
        elif isinstance(deliver_until, int):
            try:
                deliver_date = datetime.fromtimestamp(deliver_until / 1000)
                formatted_date = deliver_date.strftime('%d.%m.%Y')
            except Exception:
                try:
                    deliver_date = datetime.fromtimestamp(deliver_until)
                    formatted_date = deliver_date.strftime('%d.%m.%Y')
                except Exception:
                    logger.error(f"Не удалось обработать timestamp {deliver_until}")

    items_info = []
    total_amount = 0
    for item in order.get('orderItems', []):
        product_title = item.get('title', 'Неизвестный товар') or item.get('productTitle', 'Неизвестный товар')
        sku_char_value = item.get('skuCharValue', '')
        if sku_char_value:
            product_title += f" ({sku_char_value})"
        amount = item.get('amount', 0)
        total_amount += amount

        image_url = None
        product_image = item.get('photo', {})
        if 'photo' in product_image:
            photo_data = product_image.get('photo', {})
            try:
                if photo_data and '800' in photo_data and 'high' in photo_data['800']:
                    image_url = photo_data['800']['high']
            except (KeyError, TypeError) as e:
                logger.error(f"Ошибка при обработке фото: {e}")

        if not image_url and item.get('productImage'):
            try:
                product_image = item.get('productImage', {})
                if 'photo' in product_image:
                    photo_data = product_image.get('photo', {})
                    for size in ['800', '720', '540', '480', '240']:
                        if size in photo_data and 'high' in photo_data[size]:
                            image_url = photo_data[size]['high']
                            break
            except (KeyError, TypeError) as e:
                logger.error(f"Ошибка при обработке productImage: {e}")

        items_info.append({'title': product_title, 'amount': amount, 'image_url': image_url})

    message = f"📦 *Новый заказ №{order_id}*\n\n"
    for idx, item in enumerate(items_info, 1):
        message += f"{idx}. *{item['title']}*\n   Количество: {item['amount']} шт.\n"
    message += f"\n🚚 *Доставка до:* {formatted_date}\n"
    message += f"📊 *Общее количество товаров:* {total_amount} шт.\n"
    message += f"🆔 *ID заказа:* {order_id}"

    valid_image_urls = [item['image_url'] for item in items_info if item['image_url']]
    return message, valid_image_urls, items_info


async def send_telegram_notification(chat_id, message_text, image_urls=None):
    """
    Отправляет уведомление пользователю.
    Если присутствуют изображения – отправляет как фото или группу фотографий, иначе как текст.
    """
    try:
        if image_urls and len(image_urls) > 0:
            if len(image_urls) == 1:
                await bot.send_photo(chat_id, photo=image_urls[0], caption=message_text, parse_mode='Markdown')
            else:
                media_group = [types.InputMediaPhoto(media=image_urls[0], caption=message_text, parse_mode='Markdown')]
                for url in image_urls[1:]:
                    if url:
                        media_group.append(types.InputMediaPhoto(media=url))
                await bot.send_media_group(chat_id=chat_id, media=media_group)
        else:
            await bot.send_message(chat_id, text=message_text, parse_mode='Markdown')
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления в Telegram: {e}")
        try:
            await bot.send_message(chat_id, text=f"{message_text}\n\n⚠️ Не удалось загрузить изображения",
                                   parse_mode='Markdown')
            return True
        except Exception as text_error:
            logger.error(f"Ошибка при отправке текстового сообщения: {text_error}")
            return False


async def clear_product_ids():
    """Очищает список ID продуктов каждые 48 часов"""
    global PRODUCT_IDS
    logger.info("Очистка списка ID продуктов...")
    PRODUCT_IDS.clear()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """
    Обработчик команды /start.
    Если у пользователя уже есть настройки, выводит справку по командам,
    иначе предлагает пройти настройку через команду /config.
    """
    chat_id = str(message.chat.id)
    config = get_user_config(chat_id)
    if config:
        await message.answer("Вы уже настроили бота. Доступные команды:\n"
                             "/config — обновить настройки\n"
                             "/check — проверить заказы\n"
                             "/report — получить отчет")
    else:
        await message.answer("Привет! Добро пожаловать в бота.\n"
                             "Для начала настройте API ключ и время ежедневного отчета командой /config")


@dp.message(Command("config"))
async def cmd_config(message: types.Message,state: FSMContext):
    """
    Обработчик команды /config.
    Запускает процесс настройки, запрашивая сначала API ключ, затем время отчета.
    """
    await message.answer("Введите ваш API ключ:")
    await state.set_state(ConfigStates.waiting_for_api_key)


@dp.message(ConfigStates.waiting_for_api_key)
async def process_api_key(message: types.Message, state: FSMContext):
    """
    Получает API ключ от пользователя и переходит к запросу времени отчета.
    """
    api_key = message.text.strip()
    await state.update_data(api_key=api_key)
    await message.answer("Введите время ежедневного отчета в формате ЧЧ:ММ (например, 16:00):")
    await state.set_state(ConfigStates.waiting_for_report_time)


@dp.message(ConfigStates.waiting_for_report_time)
async def process_report_time(message: types.Message, state: FSMContext):
    """
    Обрабатывает время отчета, сохраняет настройки в БД и завершает FSM.
    """
    time_str = message.text.strip()
    try:
        report_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        await message.answer("Неверный формат времени. Попробуйте еще раз (например, 16:00):")
        return
    data = await state.get_data()
    api_key = data.get('api_key')
    chat_id = str(message.chat.id)
    save_user_config(chat_id, api_key, report_time)
    await message.answer(f"Настройки сохранены!\nAPI ключ: {api_key}\nВремя отчета: {report_time.strftime('%H:%M')}")
    await state.clear()


@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    """
    Обработчик команды /check.
    Проверяет заказы через API с использованием сохраненного API ключа и отправляет уведомления.
    """
    chat_id = str(message.chat.id)
    config = get_user_config(chat_id)
    if not config:
        await message.answer("Настройки не найдены. Настройте бота через команду /config")
        return

    orders_data = get_orders(config.api_key)
    if not orders_data:
        await message.answer("Не удалось получить данные о заказах")
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    if not orders:
        await message.answer("Нет новых заказов для обработки")
        return

    for order in orders:
        message_text, image_urls, _ = format_order_message(order)
        success = await send_telegram_notification(chat_id, message_text, image_urls)
        if success:
            await message.answer(f"✅ Уведомление о заказе {order.get('id')} успешно отправлено")
        else:
            await message.answer(f"❌ Не удалось отправить уведомление о заказе {order.get('id')}")


@dp.message(Command("report"))
async def cmd_report(message: types.Message):
    """
    Обработчик команды /report.
    Отправляет ежедневный отчет для пользователя.
    """
    chat_id = str(message.chat.id)
    config = get_user_config(chat_id)
    if not config:
        await message.answer("Настройки не найдены. Настройте бота через команду /config")
        return

    await send_daily_report(chat_id, config.api_key)


# ---------------------------- Ежедневный отчет и периодические задачи ----------------------------

async def send_daily_report(chat_id: str, api_key: str):
    """
    Формирует и отправляет ежедневный отчет пользователю.
    """
    orders_data = get_orders(api_key)
    if not orders_data:
        await bot.send_message(chat_id, "Не удалось получить данные о заказах для отчета.")
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    total_orders = len(orders)
    total_items = 0

    for order in orders:
        # Получаем информацию по заказу
        _, _, items_info = format_order_message(order)
        new_items = []
        for item in items_info:
            product_key = item['title']  # здесь используется название товара как идентификатор
            if product_key not in PRODUCT_IDS:
                new_items.append(item)
                PRODUCT_IDS.add(product_key)

        if new_items:
            new_message = f"📦 *Новый заказ №{order.get('id')}*\n\n"
            for idx, item in enumerate(new_items, 1):
                new_message += f"{idx}. *{item['title']}*\n   Количество: {item['amount']} шт.\n"
            new_message += f"\n🚚 *Доставка до:* {datetime.now().strftime('%d.%m.%Y')}\n"
            new_message += f"📊 *Общее количество товаров:* {len(new_items)} шт.\n"
            new_message += f"🆔 *ID заказа:* {order.get('id')}"
            valid_image_urls = [item['image_url'] for item in new_items if item['image_url']]
            await send_telegram_notification(chat_id, new_message, valid_image_urls)


async def periodic_check():
    """
    Периодическая проверка заказов для всех пользователей.
    Каждые 60 секунд происходит запрос к API и отправка уведомлений, если есть новые заказы.
    """
    logger.info("Запуск периодической проверки заказов для всех пользователей...")
    session = get_session()
    configs = session.query(UserConfig).all()
    session.close()
    for config in configs:
        orders_data = get_orders(config.api_key)
        if not orders_data:
            await bot.send_message(config.chat_id, "Не удалось получить данные о заказах")
            continue
        orders = orders_data.get('payload', {}).get('orders', [])
        if not orders:
            continue
        for order in orders:
            # Получаем информацию по заказу
            _, _, items_info = format_order_message(order)
            new_items = []
            for item in items_info:
                product_key = order.get('id')  # здесь используется название товара как идентификатор
                if product_key not in PRODUCT_IDS:
                    new_items.append(item)
                    PRODUCT_IDS.add(product_key)

            if new_items:
                new_message = f"📦 *Новый заказ №{order.get('id')}*\n\n"
                for idx, item in enumerate(new_items, 1):
                    new_message += f"{idx}. *{item['title']}*\n   Количество: {item['amount']} шт.\n"
                new_message += f"\n🚚 *Доставка до:* {datetime.now().strftime('%d.%m.%Y')}\n"
                new_message += f"📊 *Общее количество товаров:* {len(new_items)} шт.\n"
                new_message += f"🆔 *ID заказа:* {order.get('id')}"
                valid_image_urls = [item['image_url'] for item in new_items if item['image_url']]
                await send_telegram_notification(config.chat_id, new_message, valid_image_urls)


async def schedule_daily_reports():
    """
    Проверяет каждую минуту, совпадает ли время отправки отчета с настроенным временем для каждого пользователя,
    и отправляет отчет, если время совпало.
    """
    logger.info("Проверка времени для отправки ежедневных отчетов...")
    session = get_session()
    configs = session.query(UserConfig).all()
    session.close()
    now = datetime.now(timezone('Asia/Tashkent'))
    for config in configs:
        if now.time().strftime("%H:%M") == config.report_time.strftime("%H:%M"):
            await send_daily_report(config.chat_id, config.api_key)


async def main():
    """Основная функция запуска бота"""
    await bot.delete_webhook(drop_pending_updates=True)

    scheduler = AsyncIOScheduler(timezone=timezone('Asia/Tashkent'))  # Установка временной зоны
    scheduler.add_job(
        periodic_check,
        "interval",
        seconds=CHECK_INTERVAL
    )
    scheduler.add_job(
        clear_product_ids,
        "interval",
        hours=48  # Очистка каждые 48 часов
    )
    scheduler.add_job(
        schedule_daily_reports,
        "cron",
        hour=16, minute=0  # Ежедневно в 16:00 по ташкентскому времени
    )
    scheduler.start()

    # Запуск бота
    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
