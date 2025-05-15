#!/bin/bash

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

print_logo() {
    echo -e "${BLUE}"
    echo "┌─────────────────────────────────────┐"
    echo "│                                     │"
    echo "│        🕵️‍♂️ TELEGRAM SPY BOT        │"
    echo "│                                     │"
    echo "└─────────────────────────────────────┘"
    echo -e "${NC}"
}

print_step() {
    echo -e "${YELLOW}==>${NC} $1"
}

print_error() {
    echo -e "${RED}ОШИБКА:${NC} $1"
}

print_success() {
    echo -e "${GREEN}УСПЕХ:${NC} $1"
}

show_spinner() {
    local pid=$1
    local delay=0.1
    local spinstr='|/-\'
    while [ "$(ps a | awk '{print $1}' | grep $pid)" ]; do
        local temp=${spinstr#?}
        printf " [%c]  " "$spinstr"
        local spinstr=$temp${spinstr%"$temp"}
        sleep $delay
        printf "\b\b\b\b\b\b"
    done
    printf "    \b\b\b\b"
}

clear
print_logo
echo -e "${BLUE}Установка и настройка Telegram Spy Bot${NC}"
echo "------------------------------------------------"

print_step "Проверка зависимостей..."

if ! command -v docker &> /dev/null; then
    print_error "Docker не установлен. Устанавливаем..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    if ! command -v docker &> /dev/null; then
        print_error "Не удалось установить Docker. Посетите https://docs.docker.com/get-docker/"
        exit 1
    fi
else
    print_success "Docker уже установлен"
fi

if ! command -v docker-compose &> /dev/null; then
    print_error "Docker Compose не установлен. Устанавливаем..."
    sudo curl -L "https://github.com/docker/compose/releases/download/v2.12.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    if ! command -v docker-compose &> /dev/null; then
        print_error "Не удалось установить Docker Compose. Посетите https://docs.docker.com/compose/install/"
        exit 1
    fi
else
    print_success "Docker Compose уже установлен"
fi

echo "------------------------------------------------"

print_step "Настройка бота"

while true; do
    echo -e -n "${BLUE}Введите API токен бота:${NC} "
    read BOT_TOKEN
    
    if [ -z "$BOT_TOKEN" ]; then
        print_error "Токен не может быть пустым"
        continue
    fi
    
    if [[ ! $BOT_TOKEN =~ ^[0-9]+:[a-zA-Z0-9_-]+$ ]]; then
        print_error "Неверный формат токена. Формат должен быть: 123456789:ABCDefGhIJKlmNoPQRsTUVwxyZ"
        continue
    fi
    
    break
done

while true; do
    echo -e -n "${BLUE}Введите ваш Telegram ID:${NC} "
    read USER_ID
    
    if [ -z "$USER_ID" ]; then
        print_error "ID не может быть пустым"
        continue
    fi
    
    if [[ ! $USER_ID =~ ^[0-9]+$ ]]; then
        print_error "ID должен содержать только цифры"
        continue
    fi
    
    break
done

while true; do
    echo -e -n "${BLUE}Введите время хранения сообщений в часах [24]:${NC} "
    read MESSAGES_LIFETIME
    
    if [ -z "$MESSAGES_LIFETIME" ]; then
        MESSAGES_LIFETIME=24
        break
    fi
    
    if [[ ! $MESSAGES_LIFETIME =~ ^[0-9]+$ ]]; then
        print_error "Время должно быть числом"
        continue
    fi
    
    break
done

while true; do
    echo -e -n "${BLUE}Введите интервал очистки в секундах [3600]:${NC} "
    read CLEANUP_INTERVAL
    
    if [ -z "$CLEANUP_INTERVAL" ]; then
        CLEANUP_INTERVAL=3600
        break
    fi
    
    if [[ ! $CLEANUP_INTERVAL =~ ^[0-9]+$ ]]; then
        print_error "Интервал должен быть числом"
        continue
    fi
    
    break
done

echo "------------------------------------------------"
print_step "Создание файла конфигурации..."

# Создание .env файла
cat > .env << EOF
TOKEN=$BOT_TOKEN
USER_ID=$USER_ID
MESSAGES_LIFETIME=$MESSAGES_LIFETIME
CLEANUP_INTERVAL=$CLEANUP_INTERVAL
EOF

print_success "Файл .env создан"

print_step "Очистка предыдущей установки..."
docker-compose down 2>/dev/null
docker volume rm telegram-spybot-media telegram-spybot-db 2>/dev/null || true

echo "------------------------------------------------"
print_step "Запуск бота..."
echo -e "${YELLOW}Это может занять несколько минут...${NC}"

docker-compose up --build -d &
PID=$!
show_spinner $PID

if [ $? -eq 0 ]; then
    echo "------------------------------------------------"
    print_success "Бот успешно запущен!"
    echo -e "${GREEN}Проверьте работу бота в Telegram${NC}"
    echo ""
    echo -e "Для просмотра логов используйте: ${BLUE}docker-compose logs -f${NC}"
    echo -e "Для остановки бота: ${BLUE}docker-compose down${NC}"
    echo -e "Для перезапуска: ${BLUE}docker-compose restart${NC}"
    echo "------------------------------------------------"
else
    echo "------------------------------------------------"
    print_error "Ошибка при запуске бота"
    echo -e "Проверьте логи: ${BLUE}docker-compose logs${NC}"
    echo "------------------------------------------------"
    exit 1
fi 