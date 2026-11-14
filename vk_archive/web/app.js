"use strict";

(() => {
  const PAGE_SIZE = 100;
  const NAVIGATION = [
    ["profile", "Профиль", "profile"],
    ["conversation", "Мессенджер", "messages"],
    ["friend", "Друзья", "people"],
    ["group", "Сообщества", "groups"],
    ["photo", "Фото", "photo"],
    ["audio", "Музыка", "music"],
    ["video", "Видео", "video"],
    ["album", "Альбомы", "album"],
    ["separator"],
    ["bookmark", "Закладки", "bookmark"],
    ["doc", "Документы", "document"],
    ["gift", "Подарки", "gift"],
    ["subscription", "Подписки", "groups"],
    ["follower", "Подписчики", "people"],
    ["separator"],
    ["report", "Отчёт", "report"],
  ];
  const LABELS = Object.fromEntries(NAVIGATION.filter(([key]) => key !== "separator").map(([key, label]) => [key, label]));
  const ICONS = {
    profile: ["M20 21a8 8 0 0 0-16 0", "M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z"],
    messages: ["M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-3 2v-9.5A9.5 9.5 0 0 1 10.5 3H13a8 8 0 0 1 8 8.5Z"],
    people: ["M16 21a6 6 0 0 0-12 0", "M10 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M20 21a6 6 0 0 0-3-5", "M17 4a4 4 0 0 1 0 8"],
    groups: ["M15 20a5 5 0 0 0-10 0", "M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z", "M2 15a4 4 0 0 1 2-2", "M19 20a4 4 0 0 0-2-4", "M17 5a3 3 0 0 1 0 6"],
    photo: ["M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z", "M3 17l6-6 5 5 3-3 4 4", "M16 7h.01"],
    album: ["M4 7h16v14H4Z", "M7 3h10", "M5 5h14", "M4 17l5-5 4 4 3-3 4 4"],
    music: ["M9 17V5l12-2v12", "M9 5l12-2v4L9 9", "M9 17a3 3 0 1 1-3-3c1.7 0 3 1.3 3 3Z", "M21 15a3 3 0 1 1-3-3c1.7 0 3 1.3 3 3Z"],
    video: ["M4 3h16v18H4Z", "M9 8l7 4-7 4V8Z"],
    bookmark: ["M6 3h12v19l-6-4-6 4V3Z"],
    document: ["M5 3h9l5 5v13H5V3Z", "M14 3v6h5", "M8 13h8", "M8 17h6"],
    gift: ["M3 9h18v4H3Z", "M5 13v8h14v-8", "M12 9v12", "M12 9H8a3 3 0 1 1 3-3l1 3Z", "M12 9h4a3 3 0 1 0-3-3l-1 3Z"],
    report: ["M4 3h16v18H4Z", "M8 8h8", "M8 12h8", "M8 16h5"],
    search: ["M17 17l5 5", "M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z"],
    back: ["M15 4l-8 8 8 8"],
  };
  const TASK_STATES = {
    pending: "В очереди", running: "Загружается", done: "Готово", finished: "Готово",
    error: "Ошибка", failed: "Ошибка", partial: "Частично", interrupted: "Остановлено",
    unsupported: "Не поддерживается", unavailable: "Недоступно", skipped: "Пропущено",
  };
  const main = document.getElementById("main");
  const navigation = document.getElementById("navigation");
  const progress = document.getElementById("progress");
  const refreshButton = document.getElementById("refresh");
  const statusDot = document.getElementById("status-dot");
  const actors = new Map();
  const assets = new Map();
  const assetsByURL = new Map();
  const conversationTitles = new Map();
  let route = readRoute();
  let ownerID = 0;
  let renderSequence = 0;
  let rendering = false;
  let renderController = null;
  let statusSignature = "";
  let latestStatus = null;
  let polling = false;

  function node(tag, className = "", text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function append(parent, ...children) {
    for (const child of children) if (child) parent.append(child);
    return parent;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(key, value);
    for (const data of ICONS[name] || ICONS.document) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", data);
      svg.append(path);
    }
    return svg;
  }

  function button(text, action, className = "button") {
    const element = node("button", className, text);
    element.type = "button";
    element.addEventListener("click", action);
    return element;
  }

  function textBlock(text, className = "text") {
    return text ? node("p", className, text) : null;
  }

  function safeExternalLink(url, label) {
    try {
      const parsed = new URL(url);
      if (!["https:", "http:"].includes(parsed.protocol)) return node("span", "muted", label);
      const link = node("a", "", label || parsed.href);
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    } catch {
      return node("span", "muted", label || "Адрес не сохранён");
    }
  }

  function localURL(value) {
    if (!value) return "";
    try {
      const url = new URL(value, window.location.origin);
      return url.origin === window.location.origin && url.pathname.startsWith("/media/") ? url.pathname + url.search : "";
    } catch {
      return "";
    }
  }

  function absorb(data) {
    for (const [id, value] of Object.entries(data.actors || {})) actors.set(String(id), value);
    for (const asset of Array.isArray(data.assets) ? data.assets : []) {
      assets.set(asset.key, asset);
      if (asset.url) assetsByURL.set(asset.url, asset);
    }
    return data;
  }

  async function api(path, signal) {
    const response = await fetch("/api/" + path, { signal, credentials: "same-origin", cache: "no-store" });
    if (!response.ok) {
      let message = "Ошибка локального просмотрщика: " + response.status;
      try { const error = await response.json(); if (typeof error.error === "string") message = error.error; } catch { /* Сохраняем сообщение с кодом ответа. */ }
      throw new Error(message);
    }
    return absorb(await response.json());
  }

  function objectQuery(kind, values = {}) {
    const params = new URLSearchParams({ kind, ...Object.fromEntries(Object.entries(values).filter(([, value]) => value !== "" && value !== null && value !== undefined)) });
    return "objects?" + params.toString();
  }

  function readRoute() {
    const [rawView, rawQuery = ""] = window.location.hash.slice(1).split("?");
    const query = new URLSearchParams(rawQuery);
    const view = Object.hasOwn ? (Object.hasOwn(LABELS, rawView) ? rawView : "profile") : (Object.prototype.hasOwnProperty.call(LABELS, rawView) ? rawView : "profile");
    const offset = Number(query.get("offset") || 0);
    return {
      view,
      peer: view === "conversation" && /^-?\d+$/.test(query.get("peer") || "") ? query.get("peer") : "",
      parent: query.get("parent") || "",
      offset: Number.isSafeInteger(offset) && offset >= 0 ? offset : 0,
      q: query.get("q") || "",
      year: /^\d{4}$/.test(query.get("year") || "") ? query.get("year") : "",
    };
  }

  function routeURL(value) {
    const params = new URLSearchParams();
    for (const key of ["peer", "parent", "offset", "q", "year"]) if (value[key]) params.set(key, value[key]);
    return "#" + value.view + (params.size ? "?" + params.toString() : "");
  }

  function navigate(view, values = {}) {
    route = { view, peer: "", parent: "", offset: 0, q: "", year: "", ...values };
    window.history.pushState(null, "", routeURL(route));
    render();
  }

  function updateRoute(values) {
    navigate(route.view, { ...route, ...values });
  }

  function unwrap(value) {
    return value && typeof value === "object" && value.kind && value.data ? value.data : value;
  }

  function actor(id) {
    return actors.get(String(id)) || { name: Number(id) < 0 ? "Название не сохранено" : "Имя не сохранено", avatar: "", data: {} };
  }

  function actorName(id) {
    return id ? actor(id).name || "Имя не сохранено" : "Отправитель не указан";
  }

  function pictureURL(value) {
    if (!value || typeof value !== "object") return "";
    const sizes = ["sizes", "images", "images_with_background"].flatMap(key => Array.isArray(value[key]) ? value[key] : []).filter(item => item && (item.url || item.src));
    if (sizes.length) {
      sizes.sort((a, b) => (Number(b.width) || 0) * (Number(b.height) || 0) - (Number(a.width) || 0) * (Number(a.height) || 0));
      return sizes[0].url || sizes[0].src;
    }
    for (const key of ["photo_max_orig", "photo_200_orig", "photo_200", "photo_100", "photo_50", "thumb_256", "thumb_96", "thumb_48", "cover_src_big", "cover_src"]) if (typeof value[key] === "string") return value[key];
    return "";
  }

  function initials(name) {
    return (name || "?").trim().split(/\s+/).slice(0, 2).map(part => Array.from(part)[0] || "").join("").toLocaleUpperCase("ru-RU");
  }

  function avatar(id, options = {}) {
    const info = actor(id);
    const label = options.name || info.name || "Имя не сохранено";
    const asset = assetsByURL.get(options.url || info.avatar);
    const url = localURL(asset && asset.local);
    const element = node(url ? "img" : "span", "avatar" + (options.size ? " " + options.size : ""));
    if (url) {
      element.src = url;
      element.alt = label;
      element.loading = "lazy";
    } else {
      element.textContent = initials(label);
      element.setAttribute("aria-hidden", "true");
    }
    return element;
  }

  function date(timestamp, short = false) {
    if (!timestamp) return "";
    const value = new Date(Number(timestamp) * 1000);
    if (Number.isNaN(value.getTime())) return "";
    return short ? value.toLocaleDateString("ru-RU") : value.toLocaleString("ru-RU", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function bytes(size) {
    if (!Number(size)) return "";
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    const power = Math.min(Math.floor(Math.log(Number(size)) / Math.log(1024)), units.length - 1);
    return (Number(size) / 1024 ** power).toLocaleString("ru-RU", { maximumFractionDigits: power ? 1 : 0 }) + " " + units[power];
  }

  function rawData(value) {
    const details = node("details", "source-data");
    append(details, node("summary", "", "Сохранённые данные"), node("pre", "", JSON.stringify(value, null, 2)));
    return details;
  }

  function fileLocation(asset) {
    if (!asset) return null;
    const paths = new Set();
    if (typeof asset.path === "string" && asset.path) paths.add(asset.path);
    for (const source of Array.isArray(asset.sources) ? asset.sources : []) if (source.path) paths.add(source.path);
    if (!paths.size) return null;
    const details = node("details", "source-data");
    append(details, node("summary", "", "Где лежит файл"), node("pre", "", Array.from(paths).join("\n")));
    return details;
  }

  function mediaReference(kind, value) {
    return kind + ":" + (value.owner_id ?? value.from_id ?? 0) + "_" + (value.id ?? 0);
  }

  function mediaAsset(kind, value) {
    const canonical = kind === "sticker" ? "sticker:" + (value.sticker_id ?? value.id ?? 0) : mediaReference(kind, value);
    let asset = assets.get(canonical);
    let url = "";
    if (kind === "photo" || kind === "sticker" || kind === "image") url = pictureURL(value) || value.url;
    if (kind === "voice") url = value.link_mp3 || value.link_ogg;
    if (kind === "doc" || kind === "audio") url = value.url;
    asset = asset || assetsByURL.get(url);
    if (!asset && kind === "video") {
      const files = value.files || {};
      for (const key of Object.keys(files).filter(key => /^mp4_\d+$/.test(key)).sort((a, b) => Number(b.slice(4)) - Number(a.slice(4)))) {
        asset = assetsByURL.get(files[key]);
        if (asset) break;
      }
      asset = asset || assetsByURL.get(value.player);
    }
    return asset;
  }

  function mediaState(asset) {
    if (!asset) return "Файл не сохранён. Подробности о выгрузке — в отчёте.";
    if (asset.status === "pending" || asset.status === "downloading" || asset.status === "queued") return "Файл ещё загружается.";
    return asset.error || (asset.status === "done" ? "Локальный файл не найден." : "Файл недоступен для сохранения.");
  }

  function downloadLink(asset, name = "Скачать файл") {
    const url = localURL(asset && asset.local);
    if (!url) return null;
    const link = node("a", "attachment-download", name);
    link.href = url;
    link.download = asset.name || "";
    return link;
  }

  function renderMedia(kind, value) {
    const container = node("div", "attachment");
    const asset = mediaAsset(kind, value);
    const url = localURL(asset && asset.local);
    const title = value.title || (kind === "voice" ? "Голосовое сообщение" : kind === "video" ? "Видео" : kind === "audio" ? "Аудиозапись" : kind === "doc" ? "Документ" : "");
    if (kind === "doc") {
      const card = node("div", "document");
      append(card, node("span", "document-icon", value.ext || "файл"));
      const body = node("div", "grow");
      append(body, node("div", "document-name", title), textBlock(bytes(asset && asset.size || value.size), "document-info"));
      if (url) body.append(downloadLink(asset, "Открыть / скачать"));
      else body.append(node("p", "document-info", mediaState(asset)));
      append(card, body);
      append(container, card, fileLocation(asset));
      return container;
    }
    if (title) container.append(node("div", "attachment-title", (value.artist ? value.artist + " — " : "") + title));
    if (url && ["photo", "sticker", "image"].includes(kind)) {
      const link = node("a", "image-link");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = "Открыть сохранённое изображение";
      const image = node("img", "attachment-image");
      image.src = url;
      image.alt = value.text || title || (kind === "sticker" ? "Стикер" : "Фотография");
      image.loading = "lazy";
      append(container, append(link, image));
    } else if (url && ["video", "audio", "voice"].includes(kind)) {
      const player = node(kind === "video" ? "video" : "audio");
      player.controls = true;
      player.preload = "metadata";
      player.src = url;
      if (kind === "video") {
        const thumb = assets.get(mediaReference("video_thumb", value)) || assetsByURL.get(pictureURL({ images: value.image || [] }));
        const poster = localURL(thumb && thumb.local);
        if (poster) player.poster = poster;
      }
      append(container, player, downloadLink(asset));
      player.append(node("p", "muted", "Браузер не поддерживает этот формат. Скачайте сохранённый файл."));
    } else {
      if (kind === "video") {
        const thumb = assets.get(mediaReference("video_thumb", value)) || assetsByURL.get(pictureURL({ images: value.image || [] }));
        const poster = localURL(thumb && thumb.local);
        if (poster) {
          const image = node("img", "video-poster");
          image.src = poster;
          image.alt = title || "Обложка видео";
          container.append(image);
        }
      }
      container.append(node("div", "notice neutral", mediaState(asset)));
    }
    const location = fileLocation(asset);
    if (location) container.append(location);
    return container;
  }

  function photoTile(value) {
    const asset = mediaAsset("photo", value);
    const url = localURL(asset && asset.local);
    const tile = node(url ? "a" : "div", "photo-tile");
    if (url) {
      tile.href = url;
      tile.target = "_blank";
      tile.rel = "noopener noreferrer";
      const image = node("img");
      image.src = url;
      image.alt = value.text || "Фотография";
      image.loading = "lazy";
      tile.append(image);
    } else {
      tile.title = mediaState(asset);
      tile.append(append(node("span", "missing-picture"), icon("photo")));
    }
    return tile;
  }

  function attachment(value, depth = 0) {
    const type = value.type;
    const body = value[type] || {};
    if (["photo", "video", "audio", "doc", "sticker"].includes(type)) return renderMedia(type, body);
    if (type === "audio_message") return renderMedia("voice", body);
    if (type === "wall") return depth < 8 ? renderPost(body, depth + 1) : rawData(value);
    if (type === "wall_reply") return renderMessage(body, depth + 1);
    if (type === "link") {
      const box = node("div", "quote");
      append(box, safeExternalLink(body.url, body.title || body.url || "Ссылка"), textBlock(body.description));
      if (body.photo) box.append(renderMedia("photo", body.photo));
      return box;
    }
    if (type === "poll") {
      const box = node("div", "quote");
      box.append(node("strong", "", body.question || "Опрос"));
      for (const answer of body.answers || []) box.append(node("p", "muted text", answer.text + " — " + (answer.votes || 0)));
      return box;
    }
    if (type === "graffiti" || type === "gift") {
      const url = body.url || pictureURL(body);
      if (assetsByURL.has(url)) return renderMedia("image", { ...body, url });
    }
    const box = node("div", "quote");
    append(box, node("span", "muted small-text", "Вложение: " + (type || "неизвестный тип")), rawData(value));
    return box;
  }

  function actionText(action, fromID) {
    const member = action.member_id ? (typeof action.member_id === "number" ? actorName(action.member_id) : String(action.member_id)) : "Участник";
    const labels = {
      chat_create: "Создана беседа",
      chat_title_update: "Название беседы изменено: " + (action.text || ""),
      chat_photo_update: "Обновлена фотография беседы",
      chat_photo_remove: "Удалена фотография беседы",
      chat_invite_user: Number(action.member_id) === Number(fromID) ? member + " присоединился к беседе" : "Приглашён участник: " + member,
      chat_invite_user_by_link: member + " присоединился по ссылке",
      chat_invite_user_by_message_request: member + " присоединился к беседе",
      chat_kick_user: Number(action.member_id) === Number(fromID) ? member + " вышел из беседы" : "Исключён участник: " + member,
      chat_pin_message: "Закреплено сообщение",
      chat_unpin_message: "Сообщение откреплено",
      chat_screenshot: "Событие снимка экрана",
    };
    return labels[action.type] || "Событие беседы: " + (action.type || "неизвестно");
  }

  function renderMessage(message, depth = 0, label = "") {
    const container = node("div", depth ? "quote" : "message");
    const body = node("div", "message-body");
    if (!depth) container.append(avatar(message.from_id));
    if (label) body.append(node("span", "quote-label", label));
    const meta = node("div", "message-meta");
    append(meta, node("span", "message-name", actorName(message.from_id)), node("time", "message-time", date(message.date)));
    append(body, meta, textBlock(message.text, "text message-text"));
    if (message.action) body.append(node("div", "event", actionText(message.action, message.from_id)));
    if (message.deleted || message.is_unavailable) body.append(node("div", "notice neutral", "Сообщение удалено или недоступно."));
    if (message.reply_to_user && !message.reply_message) body.append(node("p", "reply-to", "Ответ: " + actorName(message.reply_to_user)));
    for (const item of message.attachments || []) body.append(attachment(item, depth));
    if (depth < 8) {
      if (message.reply_message) body.append(renderMessage(message.reply_message, depth + 1, "Ответ на сообщение"));
      for (const item of message.fwd_messages || []) body.append(renderMessage(item, depth + 1, "Пересланное сообщение"));
    } else if (message.reply_message || (message.fwd_messages || []).length) {
      body.append(rawData(message));
    }
    if (message.geo && message.geo.coordinates) {
      const point = message.geo.coordinates;
      const coordinates = typeof point === "string" ? point : [point.latitude, point.longitude].filter(v => v !== undefined).join(", ");
      if (coordinates) body.append(node("p", "muted small-text", "Координаты: " + coordinates));
    }
    if (message.update_time) body.append(node("span", "message-edited", "Изменено: " + date(message.update_time)));
    if (!depth) body.append(rawData(message));
    return append(container, body);
  }

  function renderPost(post, depth = 0) {
    const container = node("article", depth ? "quote" : "card");
    const authorID = post.from_id || post.owner_id;
    const header = node("div", "post-head");
    append(header, avatar(authorID), append(node("div", "grow"), node("div", "post-author", actorName(authorID)), node("div", "post-date", date(post.date))));
    append(container, header, textBlock(post.text));
    const status = post.post_source && post.post_source.data;
    const statusLabels = { profile_photo: "Обновил фото профиля", profile_activity: "Обновил статус" };
    if (statusLabels[status]) container.append(node("span", "post-status", statusLabels[status]));
    for (const item of post.attachments || []) container.append(attachment(item, depth));
    if (depth < 6) for (const item of post.copy_history || []) container.append(renderPost(item, depth + 1));
    else if ((post.copy_history || []).length) container.append(rawData(post.copy_history));
    const counters = node("div", "post-counts");
    if (post.likes) counters.append(node("span", "", "♡ " + (post.likes.count || 0)));
    if (post.comments) counters.append(node("span", "", "Комментарии: " + (post.comments.count || 0)));
    if (post.reposts) counters.append(node("span", "", "Поделились: " + (post.reposts.count || 0)));
    if (counters.childElementCount) container.append(counters);
    if (!depth && post.id && post.owner_id) {
      const comments = node("div", "comments-area");
      const list = node("div");
      let offset = 0;
      const loadButton = button("Показать сохранённые комментарии", async () => {
        loadButton.disabled = true;
        try {
          const data = await api(objectQuery("comment", { parent: post.owner_id + "_" + post.id, offset, limit: PAGE_SIZE, order: "asc" }));
          for (const row of data.items || []) list.append(renderMessage(unwrap(row)));
          offset += (data.items || []).length;
          if (!offset) list.append(node("p", "muted small-text", "Комментарии не сохранены. Доступность раздела можно проверить в отчёте."));
          loadButton.textContent = offset < data.total ? "Ещё комментарии" : "Показаны сохранённые комментарии";
          loadButton.disabled = offset >= data.total;
        } catch (error) {
          list.append(node("p", "danger small-text", error.message));
          loadButton.disabled = false;
        }
      }, "button button-quiet button-small");
      append(comments, loadButton, list);
      container.append(comments);
    }
    container.append(rawData(post));
    return container;
  }

  function personID(kind, value) {
    const isGroup = kind === "group" || (kind === "subscription" && ["group", "page", "event"].includes(value.type)) || (kind === "subscription" && value.name && !value.first_name);
    return isGroup ? -Math.abs(value.id) : value.id;
  }

  function personCard(kind, value) {
    const id = personID(kind, value);
    const card = node("article", "card person-card");
    const savedName = value.name || [value.first_name, value.last_name].filter(Boolean).join(" ") || actorName(id);
    append(card, avatar(id, { size: "friend", name: savedName, url: pictureURL(value) }), node("h3", "", savedName), textBlock(value.description), textBlock(value.city && value.city.title, "muted small-text"), rawData(value));
    return card;
  }

  function collectionCard(kind, value) {
    if (["friend", "group", "follower", "subscription"].includes(kind)) return personCard(kind, value);
    if (kind === "bookmark") return bookmarkCard(value);
    if (kind === "post") return renderPost(value);
    const card = node("article", "card");
    if (["photo", "video", "audio", "doc"].includes(kind)) {
      if (kind === "photo" && value.text) card.append(node("h3", "collection-title", value.text));
      card.append(renderMedia(kind, value));
      if (value.date) card.append(node("p", "muted small-text", date(value.date)));
    } else if (kind === "album") {
      const cover = node("div", "album-cover");
      const asset = assetsByURL.get(pictureURL(value));
      const url = localURL(asset && asset.local);
      if (url) {
        const image = node("img");
        image.src = url;
        image.alt = value.title || "Обложка альбома";
        image.loading = "lazy";
        cover.append(image);
      } else cover.append(icon("album"));
      append(card, cover, node("h3", "collection-title", value.title || "Альбом"), textBlock(value.description), value.size !== undefined ? node("p", "muted small-text", "Фотографий: " + value.size) : null, button("Открыть альбом", () => navigate("photo", { parent: "album:" + value.id }), "button button-quiet button-small"));
    } else if (kind === "gift") {
      const asset = assetsByURL.get(pictureURL(value.gift || {}));
      const url = localURL(asset && asset.local);
      if (url) {
        const image = node("img", "gift-image");
        image.src = url;
        image.alt = "Подарок";
        image.loading = "lazy";
        card.append(image);
      } else card.append(node("p", "notice neutral", "Изображение подарка не сохранено."));
      append(card, node("h3", "collection-title", value.from_id ? "От: " + actorName(value.from_id) : "Отправитель не указан"), textBlock(value.message), node("small", "muted", date(value.date)));
    } else append(card, textBlock(value.title || value.text || "Сохранённый объект"));
    card.append(rawData(value));
    return card;
  }

  function bookmarkCard(value) {
    if (value.user) return personCard("friend", value.user);
    if (value.group) return personCard("group", value.group);
    const type = value.type;
    const body = value[type] || {};
    if (type === "post") return renderPost(body);
    if (["photo", "video", "audio", "doc"].includes(type)) return collectionCard(type, body);
    const card = node("article", "card");
    append(card, node("h3", "collection-title", body.title || value.description || "Закладка"), textBlock(body.description));
    if (body.url) card.append(safeExternalLink(body.url, body.url));
    card.append(rawData(value));
    return card;
  }

  function pageHeading(title, subtitle = "") {
    const heading = node("div", "page-heading");
    append(heading, append(node("div"), node("h1", "", title), subtitle ? node("p", "subtitle", subtitle) : null));
    return heading;
  }

  function emptyState(title, message, iconName = "document") {
    const card = node("div", "card empty-state");
    append(card, append(node("div", "empty-icon"), icon(iconName)), node("h2", "", title), node("p", "", message), button("Открыть отчёт", () => navigate("report"), "button button-quiet"));
    return card;
  }

  function searchToolbar(chat = false) {
    const form = node("form", "toolbar");
    const query = node("input", "search-field");
    query.type = "search";
    query.placeholder = chat ? "Поиск в этом диалоге" : route.view === "conversation" ? "Имя собеседника или текст" : "Поиск по сохранённым данным";
    query.setAttribute("aria-label", query.placeholder);
    query.value = route.q;
    let year;
    form.append(query);
    if (chat) {
      year = node("input", "search-field year-field");
      year.type = "number";
      year.min = "2006";
      year.max = "2100";
      year.placeholder = "Год";
      year.setAttribute("aria-label", "Год сообщений");
      year.value = route.year;
      form.append(year);
    }
    const submit = node("button", "button button-quiet", "Найти");
    submit.type = "submit";
    form.append(submit);
    if (route.q || route.year) form.append(button("Сбросить", () => updateRoute({ q: "", year: "", offset: 0 }), "button button-quiet"));
    form.addEventListener("submit", event => {
      event.preventDefault();
      updateRoute({ q: query.value, year: year ? year.value : "", offset: 0 });
    });
    return form;
  }

  function pager(total, count = PAGE_SIZE) {
    const container = node("nav", "pager");
    container.setAttribute("aria-label", "Страницы результатов");
    const previous = button("← Назад", () => updateRoute({ offset: Math.max(0, route.offset - PAGE_SIZE) }), "button button-quiet");
    const next = button("Далее →", () => updateRoute({ offset: route.offset + PAGE_SIZE }), "button button-quiet");
    previous.disabled = route.offset === 0;
    next.disabled = !count || route.offset + count >= total;
    append(container, previous, node("span", "range", (total && count ? route.offset + 1 : 0) + "–" + Math.min(route.offset + count, total) + " из " + total), next);
    return container;
  }

  function personalDetails(profile) {
    const details = node("details");
    details.append(node("summary", "button-text", "Подробнее"));
    const grid = node("dl", "profile-detail-grid");
    const fields = [
      ["Город", profile.city && profile.city.title], ["Страна", profile.country && profile.country.title],
      ["Родной город", profile.home_town], ["Дата рождения", profile.bdate], ["О себе", profile.about],
      ["Деятельность", profile.activities], ["Интересы", profile.interests], ["Музыка", profile.music],
      ["Фильмы", profile.movies], ["Книги", profile.books], ["Игры", profile.games], ["Телепередачи", profile.tv],
      ["Цитаты", profile.quotes], ["Образование", profile.university_name],
    ];
    if (profile.personal) {
      const personal = profile.personal;
      const political = { 1: "Коммунистические", 2: "Социалистические", 3: "Умеренные", 4: "Либеральные", 5: "Консервативные", 6: "Монархические", 7: "Ультраконсервативные", 8: "Индифферентные", 9: "Либертарианские" };
      fields.push(["Политические взгляды", political[personal.political]], ["Мировоззрение", personal.religion], ["Вдохновляют", personal.inspired_by]);
      if (Array.isArray(personal.langs)) fields.push(["Языки", personal.langs.join(", ")]);
    }
    for (const [label, value] of fields) if (value) append(grid, node("dt", "", label), node("dd", "", value));
    if (profile.site) append(grid, node("dt", "", "Сайт"), append(node("dd"), safeExternalLink(profile.site, profile.site)));
    append(details, grid, rawData(profile));
    return details;
  }

  async function profilePage(signal, current) {
    const response = await api("profile", signal);
    if (!current()) return;
    const profile = unwrap((response.items || [])[0]) || {};
    if (!profile.id) {
      main.append(pageHeading("Профиль"), emptyState("Профиль ещё не загружен", "Запустите загрузчик или откройте существующую папку архива. Состояние выгрузки доступно в отчёте.", "profile"));
      return;
    }
    ownerID = Number(profile.id);
    const displayName = [profile.first_name, profile.last_name].filter(Boolean).join(" ") || actorName(ownerID);
    const card = node("section", "card profile-card");
    card.append(node("div", "profile-cover"));
    const summary = node("div", "profile-main");
    const information = node("div", "profile-name");
    append(information, node("h1", "", displayName), textBlock(profile.status, "profile-status text"), textBlock([profile.city && profile.city.title, profile.country && profile.country.title].filter(Boolean).join(", "), "profile-location"));
    append(summary, avatar(ownerID, { size: "large", name: displayName, url: pictureURL(profile) }), information);
    append(card, summary, append(node("div", "profile-details"), personalDetails(profile)));
    main.append(card);
    const columns = node("div", "profile-columns");
    const feed = node("div");
    const side = node("div", "profile-side");
    append(columns, feed, side);
    main.append(columns);

    const [photos, posts, friends, gifts, groups] = await Promise.all([
      api(objectQuery("photo", { parent: "photos:own", limit: 6 }), signal),
      api(objectQuery("post", { parent: "wall:" + ownerID, offset: route.offset, limit: PAGE_SIZE }), signal),
      api(objectQuery("friend", { limit: 6 }), signal),
      api(objectQuery("gift", { limit: 6 }), signal),
      api(objectQuery("group", { limit: 5 }), signal),
    ]);
    if (!current()) return;
    const photoCard = node("section", "card");
    const tabs = node("div", "media-tabs");
    for (const key of ["photo", "audio", "video", "album"]) tabs.append(button(LABELS[key], () => navigate(key), "button button-quiet"));
    photoCard.append(tabs);
    const strip = node("div", "photo-strip");
    for (const row of photos.items || []) strip.append(photoTile(unwrap(row)));
    if (strip.childElementCount) photoCard.append(strip);
    else photoCard.append(node("p", "muted small-text", "Сохранённых фотографий пока нет."));
    feed.append(photoCard);
    for (const row of posts.items || []) feed.append(renderPost(unwrap(row)));
    if (!(posts.items || []).length) feed.append(emptyState("На стене пока пусто", "Сохранённые записи появятся здесь после выгрузки. Если записи не получены, проверьте отчёт.", "profile"));
    if (posts.total > PAGE_SIZE || route.offset) feed.append(pager(posts.total, (posts.items || []).length));

    const friendCard = node("section", "card compact");
    friendCard.append(sectionLink("friend", friends.total || 0));
    const friendList = node("div", "friend-preview");
    for (const row of friends.items || []) {
      const friend = unwrap(row);
      const name = [friend.first_name, friend.last_name].filter(Boolean).join(" ") || actorName(friend.id);
      append(friendList, append(node("div", "friend-mini"), avatar(friend.id, { size: "friend", name, url: pictureURL(friend) }), node("span", "", name)));
    }
    append(friendCard, friendList);
    side.append(friendCard);

    const giftCard = node("section", "card compact");
    giftCard.append(sectionLink("gift", gifts.total || 0));
    const giftList = node("div", "gift-preview");
    for (const row of gifts.items || []) {
      const value = unwrap(row);
      const asset = assetsByURL.get(pictureURL(value.gift || {}));
      const url = localURL(asset && asset.local);
      if (url) {
        const image = node("img");
        image.src = url;
        image.alt = value.message || "Подарок";
        image.loading = "lazy";
        giftList.append(image);
      }
    }
    giftCard.append(giftList.childElementCount ? giftList : node("p", "muted small-text", gifts.total ? "Откройте список подарков" : "Подарки не сохранены"));
    side.append(giftCard);

    const groupCard = node("section", "card compact group-preview");
    groupCard.append(sectionLink("group", groups.total || 0));
    for (const row of groups.items || []) {
      const group = unwrap(row);
      append(groupCard, append(node("div", "group-mini"), avatar(-Math.abs(group.id), { size: "small", name: group.name, url: pictureURL(group) }), append(node("div", "grow"), node("strong", "", group.name || actorName(-Math.abs(group.id))), textBlock(group.description))));
    }
    if (!(groups.items || []).length) groupCard.append(node("p", "muted small-text", "Сообщества не сохранены"));
    side.append(groupCard);
  }

  function sectionLink(kind, count) {
    const header = node("div", "card-header");
    const action = button("", () => navigate(kind), "button-text section-title");
    append(action, node("strong", "", LABELS[kind]), node("span", "", count));
    return append(header, action);
  }

  function conversationTitle(value) {
    const conversation = value.conversation || {};
    const peer = conversation.peer || {};
    const title = conversation.chat_settings && conversation.chat_settings.title || actorName(peer.id);
    if (peer.id) conversationTitles.set(String(peer.id), title);
    return title;
  }

  async function conversationsPage(signal, current) {
    main.append(pageHeading("Мессенджер", "Сохранённая история личных диалогов и бесед"), searchToolbar());
    const data = await api(objectQuery("conversation", { offset: route.offset, limit: PAGE_SIZE, q: route.q }), signal);
    if (!current()) return;
    const list = node("div", "card dialog-list");
    for (const row of data.items || []) {
      const value = unwrap(row);
      const conversation = value.conversation || {};
      const peer = conversation.peer && conversation.peer.id;
      const title = conversationTitle(value);
      const last = value.last_message || {};
      const action = button("", () => navigate("conversation", { peer: String(peer) }), "dialog-row");
      const body = node("span", "grow");
      append(body, node("span", "dialog-title", title), node("span", "dialog-preview", last.text || (last.action ? actionText(last.action, last.from_id) : "Вложения или служебное сообщение")));
      append(action, avatar(peer, { name: title, url: pictureURL(conversation.chat_settings && conversation.chat_settings.photo) }), body, node("time", "dialog-time", date(last.date, true)));
      list.append(action);
    }
    if (list.childElementCount) main.append(list, pager(data.total || 0, (data.items || []).length));
    else main.append(emptyState(route.q ? "Диалоги не найдены" : "Диалоги ещё не сохранены", route.q ? "Попробуйте другое имя или фразу." : "Если загрузка уже завершилась, проверьте доступ к сообщениям в отчёте.", "messages"));
  }

  async function chatPage(signal, current) {
    const peer = Number(route.peer);
    const title = conversationTitles.get(route.peer) || (peer >= 2000000000 ? "Беседа" : actorName(peer));
    const back = button("Все диалоги", () => navigate("conversation"), "button button-quiet button-small");
    back.prepend(icon("back"));
    main.append(back);
    const heading = pageHeading(title);
    heading.classList.add("chat-page-heading");
    main.append(heading, searchToolbar(true));
    const query = { parent: route.peer, offset: route.offset, limit: PAGE_SIZE, q: route.q, order: "asc" };
    if (route.year) {
      const year = Number(route.year);
      query.after = Math.floor(new Date(year, 0, 1).getTime() / 1000);
      query.before = Math.floor(new Date(year + 1, 0, 1).getTime() / 1000);
    }
    const data = await api(objectQuery("message", query), signal);
    if (!current()) return;
    const list = node("div", "card message-list");
    for (const row of data.items || []) list.append(renderMessage(unwrap(row)));
    if (list.childElementCount) main.append(list, pager(data.total || 0, (data.items || []).length));
    else main.append(emptyState("Сообщения не найдены", route.q || route.year ? "Измените поисковый запрос или год." : "История этого диалога не получена. Проверьте отчёт.", "messages"));
  }

  async function collectionPage(signal, current) {
    const albumView = route.view === "photo" && route.parent.startsWith("album:");
    if (albumView) main.append(button("← Все альбомы", () => navigate("album"), "button button-quiet button-small"));
    main.append(pageHeading(albumView ? "Фотографии альбома" : LABELS[route.view]), searchToolbar());
    if (route.view === "audio") main.append(node("p", "notice neutral", "Здесь показаны аудиозаписи, метаданные которых сохранены в архиве. Возможность выгрузки музыкальной коллекции и файлов зависит от доступа API."));
    const data = await api(objectQuery(route.view, { offset: route.offset, limit: PAGE_SIZE, q: route.q, parent: route.parent }), signal);
    if (!current()) return;
    if (!(data.items || []).length) {
      main.append(emptyState(route.q ? "Ничего не найдено" : "Данные ещё не сохранены", route.q ? "Попробуйте другой поисковый запрос." : "Состояние раздела и причины пропусков можно посмотреть в отчёте.", (NAVIGATION.find(item => item[0] === route.view) || ["", "", "document"])[2]));
      return;
    }
    const grid = node("div", route.view === "bookmark" ? "" : "collection-grid");
    for (const row of data.items || []) grid.append(collectionCard(route.view, unwrap(row)));
    main.append(grid, pager(data.total || 0, data.items.length));
  }

  function dataTable(headings, rows) {
    const table = node("table");
    const thead = node("thead");
    const header = node("tr");
    for (const heading of headings) header.append(node("th", "", heading));
    append(table, append(thead, header));
    const tbody = node("tbody");
    for (const values of rows) {
      const row = node("tr");
      for (const value of values) row.append(append(node("td"), value && value.nodeType ? value : node("span", "", value ?? "")));
      tbody.append(row);
    }
    table.append(tbody);
    return append(node("div", "table-wrap"), table);
  }

  async function reportPage(signal, current) {
    const data = await api("status", signal);
    if (!current()) return;
    main.append(pageHeading("Отчёт о выгрузке", "Полученные разделы, сохранённые файлы и причины пропусков"));
    const counters = data.assets && !Array.isArray(data.assets) ? data.assets : {};
    const summary = node("section", "card");
    summary.append(node("p", "muted", "Отчёт описывает результат обхода API. Завершённая задача сама по себе не подтверждает полноту данных ВК."));
    const stats = node("div", "stat-grid");
    const values = [["Сохранено вложений", counters.done || 0], ["Ожидают загрузки", (counters.pending || 0) + (counters.downloading || 0)], ["Недоступно / ошибки", (counters.unavailable || 0) + (counters.failed || 0)], ["Сообщений", data.counts && data.counts.message || 0]];
    for (const [label, count] of values) stats.append(append(node("div", "stat"), node("strong", "", Number(count).toLocaleString("ru-RU")), node("span", "", label)));
    summary.append(stats);
    if (data.meta && data.meta.phase) summary.append(node("p", "small-text muted", data.meta.phase));
    main.append(summary);
    const taskCard = node("section", "card");
    taskCard.append(node("h2", "collection-title", "Разделы архива"));
    taskCard.append(dataTable(["Раздел", "Состояние", "Получено / заявлено API", "Подробности"], (data.tasks || []).map(task => [task.label || task.key, node("span", "task-status " + (task.status || ""), TASK_STATES[task.status] || task.status || ""), (task.received ?? 0) + " / " + (task.expected ?? "—"), task.error || ""])));
    if (!(data.tasks || []).length) taskCard.append(node("p", "muted", "Задачи выгрузки ещё не записаны."));
    main.append(taskCard);
    const missing = node("section", "card");
    missing.append(node("h2", "collection-title", "Недоступные файлы"));
    if ((data.missing || []).length) {
      missing.append(dataTable(["Файл", "Состояние", "Причина"], data.missing.map(item => [item.name || item.key, node("span", "task-status " + item.status, TASK_STATES[item.status] || item.status), item.error || "Причина не сохранена"])));
      missing.append(node("p", "muted small-text", "В интерфейсе может отображаться только часть списка. Полный результат сохраняется в файле report.json."));
    } else missing.append(node("p", "muted", "В текущем отчёте нет недоступных файлов."));
    main.append(missing);
    const events = node("section", "card");
    events.append(node("h2", "collection-title", "Журнал"));
    for (const event of data.events || []) append(events, append(node("div", "log-line" + (event.level === "ERROR" ? " danger" : "")), node("small", "", (event.ts || "") + (event.section ? " · " + event.section : "")), node("p", "text", event.message || "")));
    if (!(data.events || []).length) events.append(node("p", "muted", "Записей пока нет."));
    main.append(events);
    const metadata = node("details", "source-data card");
    append(metadata, node("summary", "", "Сведения об архиве"), node("pre", "", JSON.stringify(data.meta || {}, null, 2)));
    main.append(metadata);
  }

  async function render(preserveScroll = false) {
    const sequence = ++renderSequence;
    if (renderController) renderController.abort();
    renderController = new AbortController();
    const signal = renderController.signal;
    const current = () => sequence === renderSequence && !signal.aborted;
    const scroll = preserveScroll ? window.scrollY : 0;
    rendering = true;
    main.setAttribute("aria-busy", "true");
    refreshButton.disabled = true;
    main.replaceChildren(append(node("div", "card loading"), node("span", "spinner"), node("span", "", "Открытие раздела…")));
    updateNavigation();
    let cleared = false;
    const clearLoading = () => { if (!cleared && current()) { main.replaceChildren(); cleared = true; } };
    // Убираем индикатор до первого рендера, ответы остаются защищены номером запроса.
    clearLoading();
    try {
      if (route.view === "profile") await profilePage(signal, current);
      else if (route.view === "report") await reportPage(signal, current);
      else if (route.view === "conversation" && route.peer) await chatPage(signal, current);
      else if (route.view === "conversation") await conversationsPage(signal, current);
      else await collectionPage(signal, current);
    } catch (error) {
      if (current() && error.name !== "AbortError") {
        main.append(emptyState("Не удалось открыть раздел", error.message + ". Проверьте, запущен ли просмотрщик, и нажмите «Обновить».", "report"));
      }
    } finally {
      if (current()) {
        rendering = false;
        main.setAttribute("aria-busy", "false");
        refreshButton.disabled = false;
        window.scrollTo(0, scroll);
      }
    }
  }

  function updateNavigation() {
    for (const element of navigation.querySelectorAll("button[data-view]")) {
      const active = element.dataset.view === route.view;
      element.classList.toggle("active", active);
      if (active) element.setAttribute("aria-current", "page");
      else element.removeAttribute("aria-current");
      const counter = element.querySelector(".count");
      const value = latestStatus && latestStatus.counts && latestStatus.counts[element.dataset.view];
      if (counter) counter.textContent = value ? Number(value).toLocaleString("ru-RU") : "";
    }
  }

  function canRefreshAutomatically() {
    if (rendering || document.hidden) return false;
    if (document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return false;
    if (main.querySelector("details[open]")) return false;
    return !Array.from(main.querySelectorAll("audio,video")).some(player => !player.paused && !player.ended);
  }

  async function updateStatus(initial = false) {
    if (polling) return;
    polling = true;
    try {
      const data = await api("status?brief=1");
      latestStatus = data;
      const meta = data.meta || {};
      const counters = data.assets && !Array.isArray(data.assets) ? data.assets : {};
      ownerID = Number(meta.owner_id || ownerID || 0);
      const total = Object.values(counters).reduce((sum, value) => sum + (Number(value) || 0), 0);
      const state = meta.run_state || "";
      const busy = ["running", "api_finished"].includes(state);
      statusDot.className = "status-dot " + (state === "error" ? "error" : busy ? "busy" : "ready");
      const label = meta.phase || (state === "finished" ? "Архив открыт" : "Локальный архив");
      progress.textContent = label + (total ? " · вложений " + (counters.done || 0) + " / " + total : "");
      progress.title = progress.textContent;
      updateNavigation();
      const signature = JSON.stringify([meta.owner_id, data.counts, counters, data.actor_count, meta.phase, state]);
      if (initial || (signature !== statusSignature && canRefreshAutomatically())) {
        statusSignature = signature;
        await render(!initial);
      }
    } catch {
      statusDot.className = "status-dot error";
      progress.textContent = "Просмотрщик недоступен";
      progress.title = "Запустите просмотрщик для этой папки архива.";
      if (initial) await render();
    } finally {
      polling = false;
    }
  }

  for (const [key, label, iconName] of NAVIGATION) {
    if (key === "separator") { navigation.append(node("div", "nav-separator")); continue; }
    const action = button("", () => navigate(key), "nav-button");
    action.dataset.view = key;
    append(action, icon(iconName), node("span", "", label), node("span", "count"));
    navigation.append(action);
  }
  refreshButton.addEventListener("click", () => render(true));
  window.addEventListener("hashchange", () => { route = readRoute(); render(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) updateStatus(); });
  updateStatus(true);
  window.setInterval(() => updateStatus(), 4000);
})();
