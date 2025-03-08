import asyncio
import logging
import requests
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Константы
API_KEY = "mmldJYh2h33pboUakTFsohCOa1VLR5KCP4OBW0j5+y0="
TOKEN = '7279266289:AAEZhEkpNREbkFUp6DELAlWoKXEjFvc8x4Y'
CHECK_INTERVAL = 60
PRODUCT_IDS = set()

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальная переменная для хранения chat_id
CHAT_ID = None


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
    """Отправляет уведомление в Telegram с группировкой изображений"""
    try:
        if image_urls and len(image_urls) > 0:
            if len(image_urls) == 1:
                await bot.send_photo(chat_id, photo=image_urls[0], caption=message_text, parse_mode='Markdown')
            else:
                media_group = [InputMediaPhoto(media=image_urls[0], caption=message_text, parse_mode='Markdown')]
                for url in image_urls[1:]:
                    if url:
                        media_group.append(InputMediaPhoto(media=url))
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


@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Обработчик команды /start для установки chat_id"""
    global CHAT_ID
    CHAT_ID = message.chat.id
    await message.answer(
        "Бот запущен! Теперь я буду отправлять уведомления сюда.")


@dp.message(Command("check"))
async def check_new_orders_command(message: types.Message):
    """Обработчик команды /check"""
    orders_data = get_orders(API_KEY)
    if not orders_data:
        await message.answer("Не удалось получить данные о заказах")
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    if not orders:
        await message.answer("Нет новых заказов для обработки")
        return

    for order in orders:
        message_text, image_urls, items_info = format_order_message(order)
        success = await send_telegram_notification(message.chat.id, message_text, image_urls)
        if success:
            await message.answer(f"✅ Уведомление о заказе {order.get('id')} успешно отправлено")
        else:
            await message.answer(f"❌ Не удалось отправить уведомление о заказе {order.get('id')}")


async def periodic_check():
    """Периодическая проверка заказов"""
    global CHAT_ID, PRODUCT_IDS
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
        return

    for order in orders:
        message_text, image_urls, items_info = format_order_message(order)

        # Проверяем, были ли уже отправлены уведомления для этих продуктов
        new_items = []
        for item in items_info:
            product_title = item['title']
            if product_title not in PRODUCT_IDS:  # Если продукт новый
                new_items.append(item)
                PRODUCT_IDS.add(product_title)  # Добавляем его в список

        if new_items:
            # Формируем новое сообщение только для новых продуктов
            new_message = f"📦 *Новый заказ №{order.get('id')}*\n\n"
            for idx, item in enumerate(new_items, 1):
                new_message += f"{idx}. *{item['title']}*\n   Количество: {item['amount']} шт.\n"
            new_message += f"\n🚚 *Доставка до:* {datetime.now().strftime('%d.%m.%Y')}\n"
            new_message += f"📊 *Общее количество товаров:* {len(new_items)} шт.\n"
            new_message += f"🆔 *ID заказа:* {order.get('id')}"

            # Отправляем уведомление
            valid_image_urls = [item['image_url'] for item in new_items if item['image_url']]
            await send_telegram_notification(CHAT_ID, new_message, valid_image_urls)


async def clear_product_ids():
    """Очищает список ID продуктов каждые 48 часов"""
    global PRODUCT_IDS
    logger.info("Очистка списка ID продуктов...")
    PRODUCT_IDS.clear()


@dp.message(Command("report"))
async def manual_daily_report(message: types.Message):
    """Обработчик команды /report для ручного получения ежедневного отчета"""
    logger.info("Получена команда /report для ручного отчета.")
    await daily_report()
    # await message.answer("✅ Ежедневный отчет успешно отправлен!")


async def split_and_send_message(chat_id, text, max_length=4096):
    """
    Разделяет текст на части и отправляет их поочередно.
    :param chat_id: ID чата для отправки сообщений.
    :param text: Текст для отправки.
    :param max_length: Максимальная длина одной части (по умолчанию 4096).
    """
    # Разделяем текст на части
    parts = [text[i:i + max_length] for i in range(0, len(text), max_length)]

    # Отправляем каждую часть
    for part in parts:
        await bot.send_message(chat_id, text=part, parse_mode='Markdown')
        await asyncio.sleep(0.5)  # Небольшая задержка между отправками


async def daily_report():
    """Ежедневный отчет (автоматический или ручной)"""
    global CHAT_ID
    if CHAT_ID is None:
        logger.warning("CHAT_ID не установлен, пропускаю отправку ежедневного отчета.")
        return

    logger.info("Формирование ежедневного отчета...")
    orders_data = get_orders(API_KEY)
    if not orders_data:
        await bot.send_message(CHAT_ID, "Не удалось получить данные о заказах для ежедневного отчета.")
        return

    orders = orders_data.get('payload', {}).get('orders', [])
    total_orders = len(orders)
    total_items = 0
    detailed_orders = []  # Список для хранения детальной информации о заказах

    for order in orders:
        order_id = order.get('id', 'Нет данных')
        items_info = []

        for item in order.get('orderItems', []):
            product_title = item.get('title', 'Неизвестный товар') or item.get('productTitle', 'Неизвестный товар')
            amount = item.get('amount', 0)
            total_items += amount
            items_info.append(f"   - {product_title} ({amount} шт.)")

        # Формируем строку для каждого заказа
        if items_info:
            detailed_orders.append(f"📦 Заказ №{order_id}:\n" + "\n".join(items_info))

    # Формируем сообщение с общей статистикой
    report_message = (
        f"📊 *Ежедневный отчет*\n\n"
        f"📦 Всего заказов за день: {total_orders}\n"
        f"🛍️ Всего товаров: {total_items}\n\n"
    )

    # Добавляем детализированный список заказов
    if detailed_orders:
        report_message += "📋 *Список заказов:*\n" + "\n\n".join(detailed_orders)
    else:
        report_message += "⚠️ Нет данных о заказах."

    # Проверяем длину сообщения и отправляем его частями
    if len(report_message) > 4096:
        logger.info("Отчет слишком длинный, отправляю его частями...")
        await split_and_send_message(CHAT_ID, report_message)
    else:
        await bot.send_message(CHAT_ID, text=report_message, parse_mode='Markdown')

    logger.info("Ежедневный отчет успешно отправлен.")


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
