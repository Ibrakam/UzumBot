import asyncio
import logging
import requests
from datetime import datetime, date, timezone, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

engine = create_engine('sqlite:///orders.db')
Base = declarative_base()
Session = sessionmaker(bind=engine)
# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Константы
API_KEY = "mmldJYh2h33pboUakTFsohCOa1VLR5KCP4OBW0j5+y0="
TOKEN = '7679981523:AAH4dRq6FRWea24l9jd6ZB4ZpK48eQXuV64'
CHECK_INTERVAL = 60

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальная переменная для хранения chat_id
CHAT_ID = None


class SentOrder(Base):
    __tablename__ = 'sent_orders'
    order_key = Column(String, primary_key=True)
    sent_time = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)


def is_order_sent(order_key):
    session = Session()
    exists = session.query(SentOrder).filter_by(order_key=order_key).first() is not None
    logger.info(f"Заказ {order_key} уже был отправлен: {exists}")
    session.close()
    return exists


def save_order_key(order_key):
    session = Session()
    if not session.query(SentOrder).filter_by(order_key=order_key).first():
        session.add(SentOrder(order_key=order_key))
        session.commit()
    session.close()


def cleanup_old_orders_sqlalchemy():
    session = Session()
    threshold = datetime.utcnow() - timedelta(days=7)
    session.query(SentOrder).filter(SentOrder.sent_time <= threshold).delete()
    session.commit()
    session.close()


def get_orders(api_key):
    """Получение заказов через API"""
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
    """Формирует сообщение для отправки в Telegram на основе данных заказа"""
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
            except:
                try:
                    deliver_date = datetime.fromtimestamp(deliver_until)
                    formatted_date = deliver_date.strftime('%d.%m.%Y')
                except:
                    logger.error(f"Не удалось обработать timestamp {deliver_until}")

    items_info = []
    total_amount = 0
    valid_image_urls = []  # Создаем список для URL изображений здесь

    # Перебираем все товары в заказе
    for item in order.get('orderItems', []):
        product_title = item.get('title', 'Неизвестный товар') or item.get('productTitle', 'Неизвестный товар')
        sku_char_value = item.get('skuCharValue', '')
        if sku_char_value:
            product_title += f" ({sku_char_value})"
        amount = item.get('amount', 0)
        total_amount += amount

        image_url = None
        # Обрабатываем объект photo
        if item.get('photo') and isinstance(item.get('photo'), dict):
            product_image = item.get('photo')
            if 'photo' in product_image and isinstance(product_image.get('photo'), dict):
                photo_data = product_image.get('photo')
                # Проверяем наличие ключей в определенном порядке (от большего к меньшему разрешению)
                for size in ['800', '720', '540', '480', '240']:
                    if size in photo_data and 'high' in photo_data[size]:
                        image_url = photo_data[size]['high']
                        logger.info(f"Найдено изображение: {image_url}")
                        break

        # Если изображение не найдено в photo, ищем в productImage
        if not image_url and item.get('productImage'):
            try:
                product_image = item.get('productImage')
                if 'photo' in product_image:
                    photo_data = product_image.get('photo')
                    for size in ['800', '720', '540', '480', '240']:
                        if size in photo_data and 'high' in photo_data[size]:
                            image_url = photo_data[size]['high']
                            logger.info(f"Найдено изображение в productImage: {image_url}")
                            break
            except (KeyError, TypeError) as e:
                logger.error(f"Ошибка при обработке productImage: {e}")

        if image_url:
            valid_image_urls.append(image_url)

        items_info.append({
            'title': product_title,
            'amount': amount,
            'image_url': image_url,
            'skuCharValue': sku_char_value
        })

    # Логируем количество товаров и изображений
    logger.info(f"Заказ {order_id}: всего товаров: {len(items_info)}, найдено изображений: {len(valid_image_urls)}")

    message = f"📦 *Новый заказ №{order_id}*\n\n"
    for idx, item in enumerate(items_info, 1):
        message += f"{idx}. *{item['title']}*\n   Количество: {item['amount']} шт.\n"
    message += f"\n🚚 *Доставка до:* {formatted_date}\n"
    message += f"📊 *Общее количество товаров:* {total_amount} шт.\n"
    message += f"🆔 *ID заказа:* {order_id}"

    return message, valid_image_urls, items_info


async def send_telegram_notification(chat_id, message_text, image_urls=None):
    """Отправляет уведомление в Telegram с группировкой изображений"""
    try:
        # Устраняем None и пустые строки из списка урлов
        valid_urls = []
        if image_urls:
            for url in image_urls:
                if url and isinstance(url, str) and url.strip():
                    valid_urls.append(url)

        logger.info(f"Отправка сообщения с {len(valid_urls)} изображениями")

        if valid_urls:
            # Ограничиваем количество изображений до 10 (максимум для медиагруппы)
            valid_urls = valid_urls[:10]

            if len(valid_urls) == 1:
                logger.info(f"Отправка одиночного изображения: {valid_urls[0]}")
                await bot.send_photo(chat_id, photo=valid_urls[0], caption=message_text, parse_mode='Markdown')
            else:
                logger.info(f"Отправка медиагруппы из {len(valid_urls)} изображений")
                # Создаем медиагруппу
                media_group = [InputMediaPhoto(media=valid_urls[0], caption=message_text, parse_mode='Markdown')]
                for url in valid_urls[1:]:
                    media_group.append(InputMediaPhoto(media=url))

                try:
                    await bot.send_media_group(chat_id=chat_id, media=media_group)
                except Exception as media_error:
                    logger.error(f"Ошибка при отправке медиагруппы: {media_error}")
                    # В случае ошибки отправляем сначала текст
                    await bot.send_message(chat_id, text=message_text, parse_mode='Markdown')
                    # Затем отправляем изображения по одному с задержкой
                    for url in valid_urls:
                        try:
                            await asyncio.sleep(1)
                            await bot.send_photo(chat_id, photo=url)
                        except Exception:
                            logger.error(f"Не удалось отправить изображение {url}")
        else:
            await bot.send_message(chat_id, text=message_text, parse_mode='Markdown')

        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления в Telegram: {e}")
        try:
            # Пробуем отправить без форматирования в случае ошибки
            await bot.send_message(chat_id,
                                   text=f"{message_text.replace('*', '')}\n\n⚠️ Не удалось загрузить изображения")
            return True
        except Exception as text_error:
            logger.error(f"Ошибка при отправке текстового сообщения: {text_error}")
            return False


@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Обработчик команды /start для установки chat_id"""
    global CHAT_ID
    CHAT_ID = message.chat.id
    logger.info(
        f"Chat ID установлен: {CHAT_ID}, пользователь: {message.from_user.username}, название: {message.chat.title}")
    await message.answer(
        "Бот запущен! Теперь я буду отправлять уведомления сюда.")


@dp.message(Command("check"))
async def check_new_orders_command(message: types.Message):
    """Обработчик команды /check"""
    await message.answer("Начинаю проверку заказов...")

    orders_data = get_orders(API_KEY)
    if not orders_data:
        await message.answer("❌ Не удалось получить данные о заказах")
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    if not orders:
        await message.answer("📭 Нет новых заказов для обработки")
        return

    await message.answer(f"📋 Найдено {len(orders)} заказов. Начинаю отправку...")

    for order in orders:
        order_id = order.get('id')
        message_text, image_urls, items_info = format_order_message(order)

        await message.answer(f"📤 Отправка заказа #{order_id} с {len(image_urls)} изображениями...")

        try:
            # Отправляем уведомление
            success = await send_telegram_notification(message.chat.id, message_text, image_urls)

            if success:
                await message.answer(f"✅ Заказ #{order_id} успешно отправлен")
            else:
                await message.answer(f"⚠️ Проблемы при отправке заказа #{order_id}")

            # Добавляем задержку между отправками заказов
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Ошибка при отправке заказа #{order_id}: {e}")
            await message.answer(f"❌ Ошибка при отправке заказа #{order_id}: {str(e)[:100]}...")
            await asyncio.sleep(5)  # Увеличенная задержка при ошибке

    await message.answer("✅ Проверка заказов завершена")


async def periodic_check():
    """Периодическая проверка заказов с защитой от флуд контроля"""
    global CHAT_ID
    if CHAT_ID is None:
        logger.warning("CHAT_ID не установлен, пропускаю автоматическую проверку.")
        return

    logger.info("Выполняется автоматическая проверка заказов...")
    orders_data = get_orders(API_KEY)
    if not orders_data:
        await bot.send_message(CHAT_ID, "Не удалось получить данные о заказах")
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    if not orders:
        logger.info("Новых заказов не найдено")
        return

    logger.info(f"Получено {len(orders)} заказов")

    # Обработка каждого заказа отдельно
    for order in orders:
        order_id = order.get('id')
        logger.info(f"Обработка заказа #{order_id}")

        # Получаем информацию о заказе - текст сообщения и URL изображений
        message_text, image_urls, items_info = format_order_message(order)

        # Проверяем на новые товары
        new_order = False
        for item in items_info:
            # Формируем уникальный ключ для каждого товара
            sku = item.get('skuCharValue', '')
            unique_key = f"{order_id}"

            if not is_order_sent(unique_key):
                # Нашли новый товар
                new_order = True
                save_order_key(unique_key)
                logger.info(f"Новый товар: {item['title']} (SKU: {sku})")

        # Если заказ новый, отправляем уведомление
        if new_order:
            logger.info(f"Отправка уведомления о заказе #{order_id} с {len(image_urls)} изображениями")

            try:
                # Попытка отправить уведомление
                await send_telegram_notification(CHAT_ID, message_text, image_urls)
                logger.info(f"Успешно отправлено уведомление о заказе #{order_id}")
            except Exception as e:
                logger.error(f"Ошибка при отправке заказа #{order_id}: {e}")

            # Задержка между отправками разных заказов
            await asyncio.sleep(3)  # 3 секунды между заказами для избежания флуд контроля


# Чтобы не забыть добавить нужные импорты
# from aiogram.types import InputMediaPhoto
# import asyncio
# Увеличиваем задержку в случае ошибки


@dp.message(Command("report"))
async def manual_daily_report(message: types.Message):
    """Обработчик команды /report для ручного получения ежедневного отчета"""
    logger.info("Получена команда /report для ручного отчета.")
    await daily_report()
    # await message.answer("✅ Ежедневный отчет успешно отправлен!")


async def daily_report():
    """Ежедневный отчет (автоматический или ручной)"""
    global CHAT_ID
    if CHAT_ID is None:
        logger.warning("CHAT_ID не установлен, пропускаю отправку ежедневного отчета.")
        return

    logger.info("Формирование ежедневного отчета...")
    orders_data = get_orders(API_KEY)
    if not orders_data:
        await bot.send_message(
            CHAT_ID,
            "Не удалось получить данные о заказах для ежедневного отчета.",
            parse_mode=None
        )
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    today = date.today()
    today_orders = []

    for order in orders:
        created_at = order.get('dateCreated')
        if not created_at:
            logger.warning("Дата заказа отсутствует для заказа.")
            continue

        order_date = None
        # 1) Числовой таймстамп (ms)
        if isinstance(created_at, (int, float)) or (isinstance(created_at, str) and created_at.isdigit()):
            try:
                ts = int(created_at) / 1000
                # UTC-время без лишних импортов
                order_date = datetime.utcfromtimestamp(ts).date()
                # Если хотите местное время, используйте:
                # order_date = datetime.fromtimestamp(ts).date()
            except Exception as e:
                logger.warning(f"Не удалось обработать таймстамп {created_at}: {e}")

        # 2) ISO-строка
        else:
            try:
                s = str(created_at)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                order_date = datetime.fromisoformat(s).date()
            except Exception as e:
                logger.warning(f"Не удалось обработать дату заказа {created_at}: {e}")

        if order_date == today:
            today_orders.append(order)

    total_orders = len(today_orders)
    total_items = 0
    detailed_orders = []

    for order in today_orders:
        order_id = order.get('id', 'Нет данных')
        items_info = []
        for item in order.get('orderItems', []):
            title = item.get('title') or item.get('productTitle') or 'Неизвестный товар'
            title = sanitize_text(title)
            amount = item.get('amount', 0)
            total_items += amount
            items_info.append(f"   - {title} ({amount} шт.)")
        if items_info:
            detailed_orders.append(f"📦 Заказ №{order_id}:\n" + "\n".join(items_info))

    report_message = (
        f"📊 ЕЖЕДНЕВНЫЙ ОТЧЕТ\n\n"
        f"📦 Всего заказов за сегодня: {total_orders}\n"
        f"🛍️ Всего товаров: {total_items}\n\n"
    )
    if detailed_orders:
        report_message += "📋 СПИСОК ЗАКАЗОВ:\n" + "\n\n".join(detailed_orders)
    else:
        report_message += "⚠️ Нет данных о заказах за сегодня."

    logger.debug(f"Длина сообщения: {len(report_message)}")

    try:
        if len(report_message) > 4096:
            logger.info("Отчет слишком длинный, отправляю его частями...")
            await split_and_send_message(CHAT_ID, report_message)
        else:
            await bot.send_message(
                CHAT_ID,
                text=report_message,
                parse_mode=None
            )
        logger.info("Ежедневный отчет успешно отправлен.")
    except Exception as e:
        logger.error(f"Ошибка при отправке отчета: {e}")
        simple_message = f"📊 ЕЖЕДНЕВНЫЙ ОТЧЕТ: {total_orders} заказов, {total_items} товаров."
        await bot.send_message(
            CHAT_ID,
            text=simple_message,
            parse_mode=None
        )


def sanitize_text(text):
    """Очищает текст от потенциально проблемных символов"""
    if not text:
        return ""
    # Заменяем символы, которые могут быть интерпретированы как разметка
    problematic_chars = ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in problematic_chars:
        text = text.replace(char, ' ')
    return text


# Функция для разделения длинных сообщений
async def split_and_send_message(chat_id, message, max_length=4000):
    """Разделяет длинное сообщение на части и отправляет их последовательно"""
    import asyncio

    if len(message) <= max_length:
        # ВАЖНО: явно указываем parse_mode=None
        await bot.send_message(chat_id, text=message, parse_mode=None)
        return

    parts = []
    current_part = ""
    paragraphs = message.split("\n\n")

    for paragraph in paragraphs:
        # Если параграф сам по себе больше максимальной длины
        if len(paragraph) > max_length:
            # Разделяем по строкам
            lines = paragraph.split("\n")
            for line in lines:
                if len(current_part) + len(line) + 1 <= max_length:
                    if current_part:
                        current_part += "\n"
                    current_part += line
                else:
                    parts.append(current_part)
                    current_part = line
        else:
            # Добавляем параграф, если помещается
            if len(current_part) + len(paragraph) + 2 <= max_length:
                if current_part:
                    current_part += "\n\n"
                current_part += paragraph
            else:
                parts.append(current_part)
                current_part = paragraph

    if current_part:
        parts.append(current_part)

    # Отправляем каждую часть
    for i, part in enumerate(parts):
        try:
            # ВАЖНО: явно указываем parse_mode=None
            await bot.send_message(chat_id, text=part, parse_mode=None)
            # Небольшая задержка между сообщениями
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка при отправке части {i + 1}: {e}")
            logger.debug(f"Проблемная часть сообщения [{i + 1}]: {part[:50]}...")


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
        cleanup_old_orders_sqlalchemy,
        "interval",
        hours=24
    )
    scheduler.add_job(
        daily_report,
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
