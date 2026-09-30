# Mini App: собирается на Node, отдаётся nginx. Тем же nginx'ом проксируется /api на сервис `bot` —
# так проверяющему не нужен ни CORS, ни второй порт, ни переменные сборки: фронт по умолчанию зовёт
# относительный `/api/v1` (frontend/src/lib/api.js), а куда он попадёт — решает этот контейнер.
#
# Отдельный образ, а не `vite preview`: preview-сервер не предназначен для нагрузки и поднимает
# dev-зависимости, а проверять нужно ту же прод-сборку, что живёт на max.chestnyidiplom.ru.

FROM node:20-alpine AS build

WORKDIR /app

# Сначала только манифест: слой с зависимостями пересобирается при изменении package-lock.json,
# а не при каждой правке в src/. Это и есть ответ на «сборка не дороже пяти минут».
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
