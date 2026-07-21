// The whole board is drawn from socket snapshots, so a missing socket client
// (e.g. blocked script) must fail loudly instead of silently killing the page.
const socket = (typeof io !== "undefined")
    ? io()
    : { emit() {}, on() {} };

const ANIM_MS = 520;
const PICKUP_ANIM_MS = 520;
const CENTER_ANIM_MS = 760;
const SWITCH_ANIM_MS = 620;

let mySid = "";
let myUsername = "";
let state = null;
let prevState = null;
let ready = false;
let toastTimer = null;
let lastActionKey = "";
let animating = false;
/** @type {{sid: string, index: number, untilTurnSid: string}|null} */
let swapMark = null;
let recentCardMarks = {
    looked: [],
    switched: [],
    createdBySid: "",
    throughTurnSid: "",
};
let stateUpdateQueue = Promise.resolve();
let finalCountdownEndsAt = 0;
let lastBurnShowdownId = 0;
let burnShowdownTimer = null;
let activeGridPeekMode = "self";
let tableCameraCache = null;
let seatLayoutCache = { key: "", positions: [] };
let chatMessages = [];
let chatUnread = 0;
let lobbySettingsOpen = false;
let localBurnAttempt = null;
let localBurnAttemptTimer = null;
let keyboardNav = {
    ownerSid: "",
    index: 0,
    menuOpen: false,
    menuIndex: 0,
};
const roomStoragePrefix = `shmamale:${ROOM_ID}:`;
const reconnectToken = loadReconnectToken();

const els = {};

function syncAppViewportHeight() {
    const height = window.visualViewport?.height || window.innerHeight;
    if (height > 0) {
        document.documentElement.style.setProperty("--app-height", `${Math.round(height)}px`);
    }
}

if (typeof io === "undefined") {
    setConnBanner("Game client failed to load (socket.io missing). Please refresh the page.", false);
}

document.addEventListener("DOMContentLoaded", () => {
    syncAppViewportHeight();
    cacheEls();
    bindStaticEvents();
    bindCardPointerFallback();
    applyTableCameraSettings();
    initializeGameMode();
    window.setInterval(updateCountdownText, 100);
    const handleViewportChange = () => {
        syncAppViewportHeight();
        applyTableCameraSettings(true);
        if (!state) return;
        if (els.seats && state.players) renderSeats();
        else if (els.seats) layoutNameplates(rotatedOrder());
        layoutArLabels();
        layoutAbilityTray();
    };
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("orientationchange", handleViewportChange);
    window.visualViewport?.addEventListener("resize", handleViewportChange);
    window.visualViewport?.addEventListener("scroll", handleViewportChange);
});

socket.on("connect", () => {
    mySid = socket.id;
    setConnBanner("", true);
    if (myUsername && state) joinGame(myUsername, { reconnect: true });
    if (state) render();
});

socket.on("connect_error", () => {
    setConnBanner("Can't reach the game server. Make sure it is running, then wait for reconnect…", false);
});

socket.on("disconnect", () => {
    setConnBanner("Disconnected — trying to reconnect…", false);
});

socket.on("game_state", (nextState) => {
    stateUpdateQueue = stateUpdateQueue
        .then(() => applyGameState(nextState))
        .catch((error) => {
            console.error("State update failed", error);
            animating = false;
        });
});

async function applyGameState(nextState) {
    if (nextState.viewer_sid) {
        mySid = nextState.viewer_sid;
    }
    const action = nextState.last_action;
    const actionKey = action
        ? `${action.id ?? `${action.type}:${action.epoch}:${action.sid || ""}:${action.index ?? ""}:${action.owner_sid || ""}`}`
        : "";

    // Capture DOM anchors from the current board before we replace it.
    const anchors = captureAnchors();

    prevState = state;
    state = nextState;
    setChatAvailability(true);
    const hasNewBurnResult = Boolean(
        nextState.burn_showdown
        && nextState.burn_showdown.id !== lastBurnShowdownId
    );
    if (hasNewBurnResult || action?.type === "burn" || action?.type === "burn_fail") {
        clearLocalBurnAttempt();
    }
    normalizeKeyboardFocus();
    if (!canChooseNow()) keyboardNav.menuOpen = false;
    finalCountdownEndsAt = Number.isFinite(nextState.final_countdown_ends_at)
        ? nextState.final_countdown_ends_at
        : 0;
    updateSwapMark(action, nextState);
    updateRecentCardMarks(action, actionKey, nextState);

    if (action && actionKey !== lastActionKey) {
        lastActionKey = actionKey;
        if (action.type === "switch" && action.sid !== mySid) {
            const who = nextState.players[action.sid]?.username || "A player";
            showToast(`${who} switched two cards.`);
        }
        if (action.type === "peek" && action.sid !== mySid) {
            const who = nextState.players[action.sid]?.username || "A player";
            const own = action.owner_sid === action.sid;
            showToast(own ? `${who} looked at a card.` : `${who} looked at someone's card.`);
        }
        if (action.type === "burn_fail") {
            const who = nextState.players[action.sid]?.username || "A player";
            showToast(`${who} failed a burn and drew a penalty card.`);
        }
        if (action.type === "burn") {
            const who = nextState.players[action.sid]?.username || "A player";
            const owner = nextState.players[action.owner_sid]?.username || "a player";
            const target = action.owner_sid === action.sid ? "their own card" : `${owner}'s card`;
            showToast(`${who} burned ${target}.`);
        }
        render({ hideAnimTargets: true, action });
        await playActionAnimation(action, anchors);
        render();
        return;
    }
    render();
}

socket.on("error_message", (data) => {
    showToast(data.msg || "Something went wrong.");
});

socket.on("chat_history", (data) => {
    chatMessages = Array.isArray(data?.messages) ? data.messages.slice(-100) : [];
    chatUnread = 0;
    renderChat();
});

socket.on("chat_message", (message) => {
    if (!message || chatMessages.some((item) => item.id === message.id)) return;
    chatMessages.push(message);
    chatMessages = chatMessages.slice(-100);
    if (els.chatPanel?.classList.contains("hidden") && message.sender_sid !== mySid) {
        chatUnread += 1;
    }
    renderChat();
});

socket.on("burn_attempt_registered", (data) => {
    markLocalBurnAttempt(data?.owner_sid, Number(data?.index), Number(data?.contest_window_ms));
    showToast("Burn attempt registered — waiting for the server showdown…");
});

function updateSwapMark(action, nextState) {
    if (swapMark && nextState.current_turn_sid !== swapMark.untilTurnSid) {
        swapMark = null;
    }
    if (action && action.type === "swap") {
        swapMark = {
            sid: action.sid,
            index: action.index,
            untilTurnSid: nextState.current_turn_sid,
        };
    }
    if (nextState.status === "round_over" || nextState.status === "game_over") {
        swapMark = null;
    }
}

function setConnBanner(message, ok) {
    const el = document.getElementById("conn-banner");
    if (!el) return;
    if (!message) {
        el.classList.add("hidden");
        return;
    }
    el.textContent = message;
    el.classList.toggle("ok", Boolean(ok));
    el.classList.remove("hidden");
}

function cacheEls() {
    els.lobby = document.getElementById("lobby");
    els.game = document.getElementById("game");
    els.playerList = document.getElementById("player-list");
    els.lobbyColumns = document.getElementById("lobby-columns");
    els.lobbySettingsToggle = document.getElementById("lobby-settings-toggle");
    els.hostControls = document.getElementById("host-controls");
    els.settingPreset = document.getElementById("setting-preset");
    els.settingTarget = document.getElementById("setting-target");
    els.settingWinCondition = document.getElementById("setting-win-condition");
    els.settingGridRows = document.getElementById("setting-grid-rows");
    els.settingGridCols = document.getElementById("setting-grid-cols");
    els.settingJokerValue = document.getElementById("setting-joker-value");
    els.settingJokers = document.getElementById("setting-jokers");
    els.settingDeckCount = document.getElementById("setting-deck-count");
    els.settingPeekDistance = document.getElementById("setting-peek-distance");
    els.settingPeekDirection = document.getElementById("setting-peek-direction");
    els.gridRuleEditor = document.getElementById("grid-rule-editor");
    els.peekModeTools = document.getElementById("peek-mode-tools");
    els.addBotBtn = document.getElementById("add-bot-btn");
    els.readyBtn = document.getElementById("ready-btn");
    els.startBtn = document.getElementById("start-btn");
    els.turnText = document.getElementById("turn-text");
    els.phaseText = document.getElementById("phase-text");
    els.scoreChips = document.getElementById("score-chips");
    els.drawBtn = document.getElementById("draw-btn");
    els.discardBtn = document.getElementById("discard-btn");
    els.drawCount = document.getElementById("draw-count");
    els.discardCount = document.getElementById("discard-count");
    els.drawPrompt = document.getElementById("draw-prompt");
    els.takePrompt = document.getElementById("take-prompt");
    els.playPrompt = document.getElementById("play-prompt");
    els.callBtn = document.getElementById("call-btn");
    els.seats = document.getElementById("seats");
    els.hands = document.getElementById("hand-overlays");
    els.nameplates = document.getElementById("nameplates");
    els.flyLayer = document.getElementById("fly-layer");
    els.abilityOverlay = document.getElementById("ability-overlay");
    els.burnShowdown = document.getElementById("burn-showdown");
    els.actionHint = document.getElementById("action-hint");
    els.toast = document.getElementById("toast");
    els.keyboardActionMenu = document.getElementById("keyboard-action-menu");
    els.keybindsBtn = document.getElementById("keybinds-btn");
    els.keybindsOverlay = document.getElementById("keybinds-overlay");
    els.keybindsClose = document.getElementById("keybinds-close");
    els.chatToggles = [...document.querySelectorAll("[data-chat-toggle]")];
    els.chatUnread = [...document.querySelectorAll("[data-chat-unread]")];
    els.chatPanel = document.getElementById("room-chat");
    els.chatClose = document.getElementById("chat-close");
    els.chatMessages = document.getElementById("chat-messages");
    els.chatForm = document.getElementById("chat-form");
    els.chatInput = document.getElementById("chat-input");
    els.chatSend = document.getElementById("chat-send");
}

function bindStaticEvents() {
    if (els.lobbySettingsToggle) {
        els.lobbySettingsToggle.addEventListener("click", () => {
            setLobbySettingsOpen(!lobbySettingsOpen);
        });
    }
    if (els.readyBtn) {
        els.readyBtn.addEventListener("click", () => {
            ready = !ready;
            socket.emit("toggle_ready", { room: ROOM_ID });
        });
    }
    if (els.startBtn) {
        els.startBtn.addEventListener("click", () => socket.emit("start_game", { room: ROOM_ID }));
    }

    if (els.settingPreset) {
        els.settingPreset.addEventListener("change", () => {
            if (["default", "madhouse"].includes(els.settingPreset.value)) {
                socket.emit("update_settings", {
                    room: ROOM_ID,
                    preset: els.settingPreset.value,
                });
            } else {
                emitRoomSettings();
            }
        });
    }
    [
        els.settingTarget,
        els.settingWinCondition,
        els.settingJokerValue,
        els.settingJokers,
        els.settingDeckCount,
        els.settingPeekDistance,
        els.settingPeekDirection,
    ].filter(Boolean).forEach((input) => input.addEventListener("change", () => emitRoomSettings()));
    [els.settingGridRows, els.settingGridCols].filter(Boolean).forEach((input) => {
        input.addEventListener("change", resizeGridSettings);
    });
    if (els.peekModeTools) {
        els.peekModeTools.addEventListener("click", (event) => {
            const button = event.target.closest("[data-peek-mode]");
            if (!button || button.disabled) return;
            activeGridPeekMode = button.dataset.peekMode;
            renderPeekModeTools();
        });
    }
    if (els.gridRuleEditor) {
        els.gridRuleEditor.addEventListener("click", (event) => {
            const cell = event.target.closest("[data-grid-index]");
            if (!cell || cell.disabled || !state) return;
            const index = Number(cell.dataset.gridIndex);
            const modes = state.settings.grid_peek_modes.slice();
            modes[index] = modes[index] === activeGridPeekMode ? "none" : activeGridPeekMode;
            state.settings.grid_peek_modes = modes;
            state.settings.preset = "custom";
            els.settingPreset.value = "custom";
            renderGridRuleEditor();
            emitRoomSettings({ grid_peek_modes: modes });
        });
    }
    if (els.addBotBtn) {
        els.addBotBtn.addEventListener("click", () => {
            socket.emit("add_bot", { room: ROOM_ID, difficulty: "medium" });
        });
    }
    if (els.playerList) {
        els.playerList.addEventListener("change", (event) => {
            const select = event.target.closest("[data-bot-difficulty]");
            if (!select) return;
            socket.emit("update_bot_difficulty", {
                room: ROOM_ID,
                sid: select.dataset.botDifficulty,
                difficulty: select.value,
            });
        });
        els.playerList.addEventListener("click", (event) => {
            const remove = event.target.closest("[data-remove-bot]");
            if (!remove) return;
            socket.emit("remove_bot", { room: ROOM_ID, sid: remove.dataset.removeBot });
        });
    }

    if (els.drawBtn) els.drawBtn.addEventListener("click", tryDraw);
    if (els.drawPrompt) els.drawPrompt.addEventListener("click", tryDraw);
    if (els.discardBtn) els.discardBtn.addEventListener("click", onDiscardPileClick);
    if (els.takePrompt) els.takePrompt.addEventListener("click", tryTake);
    if (els.playPrompt) els.playPrompt.addEventListener("click", playDrawn);
    if (els.callBtn) els.callBtn.addEventListener("click", () => socket.emit("call_round", { room: ROOM_ID }));
    if (els.keybindsBtn) els.keybindsBtn.addEventListener("click", openKeybinds);
    if (els.keybindsClose) els.keybindsClose.addEventListener("click", closeKeybinds);
    if (els.keybindsOverlay) {
        els.keybindsOverlay.addEventListener("click", (event) => {
            if (event.target === els.keybindsOverlay) closeKeybinds();
        });
    }
    if (els.keyboardActionMenu) {
        els.keyboardActionMenu.addEventListener("click", (event) => {
            const button = event.target.closest("[data-keyboard-action]");
            if (!button || button.disabled) return;
            const actions = keyboardMenuActions();
            const index = actions.findIndex((action) => action.id === button.dataset.keyboardAction);
            if (index < 0) return;
            keyboardNav.menuIndex = index;
            confirmKeyboardMenuAction();
        });
    }
    els.chatToggles?.forEach((button) => button.addEventListener("click", openChat));
    if (els.chatClose) els.chatClose.addEventListener("click", closeChat);
    if (els.chatForm) els.chatForm.addEventListener("submit", sendChatMessage);
    if (els.chatInput) {
        els.chatInput.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                closeChat();
            }
        });
    }
    document.addEventListener("keydown", handleGameKeydown);
}

function setChatAvailability(available) {
    els.chatToggles?.forEach((button) => {
        button.disabled = !available;
    });
    if (els.chatInput) els.chatInput.disabled = !available;
    if (els.chatSend) els.chatSend.disabled = !available;
}

function openChat() {
    if (!state || !els.chatPanel) return;
    chatUnread = 0;
    els.chatPanel.classList.remove("hidden");
    els.chatToggles?.forEach((button) => button.setAttribute("aria-expanded", "true"));
    renderChat();
    els.chatInput?.focus();
}

function closeChat() {
    if (!els.chatPanel) return;
    els.chatPanel.classList.add("hidden");
    els.chatToggles?.forEach((button) => button.setAttribute("aria-expanded", "false"));
    updateChatUnread();
    const visibleToggle = els.chatToggles?.find((button) => button.offsetParent !== null);
    visibleToggle?.focus();
}

function updateChatUnread() {
    els.chatUnread?.forEach((badge) => {
        badge.textContent = String(Math.min(chatUnread, 99));
        badge.classList.toggle("hidden", chatUnread <= 0);
    });
}

function renderChat() {
    if (!els.chatMessages) return;
    if (!chatMessages.length) {
        els.chatMessages.innerHTML = '<p class="chat-empty">No messages yet.</p>';
        updateChatUnread();
        return;
    }
    els.chatMessages.innerHTML = chatMessages.map((message) => {
        const sentAt = new Date(Number(message.sent_at) || Date.now());
        const timeLabel = sentAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
        return `
            <article class="chat-message ${message.sender_sid === mySid ? "mine" : ""}">
                <div class="chat-message-meta">
                    <strong>${escapeHtml(message.username || "Player")}</strong>
                    <time datetime="${escapeHtml(sentAt.toISOString())}">${escapeHtml(timeLabel)}</time>
                </div>
                <p>${escapeHtml(message.message || "")}</p>
            </article>
        `;
    }).join("");
    updateChatUnread();
    if (!els.chatPanel?.classList.contains("hidden")) {
        els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
    }
}

function sendChatMessage(event) {
    event.preventDefault();
    if (!state || !els.chatInput) return;
    const message = els.chatInput.value.trim();
    if (!message) return;
    socket.emit("send_chat", { room: ROOM_ID, message });
    els.chatInput.value = "";
    els.chatInput.focus();
}

/** 3D CSS hit-tests are unreliable; resolve cards by stack + AABB fallback. */
function pointInRect(clientX, clientY, el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom;
}

function pileControlAtPoint(clientX, clientY) {
    const stack = document.elementsFromPoint(clientX, clientY);
    const fromStack = stack.find((el) => (
        el.classList?.contains("pile-card")
        || el.classList?.contains("prompt-label")
    ));
    if (fromStack) return fromStack;

    const candidates = [
        els.drawPrompt, els.drawBtn, els.takePrompt, els.playPrompt, els.discardBtn,
    ].filter(Boolean);
    return candidates.find((el) => !el.classList.contains("hidden") && pointInRect(clientX, clientY, el)) || null;
}

function cardAtPoint(clientX, clientY, tolerance = 0) {
    // Piles sit in the middle — never treat them as board cards.
    if (pileControlAtPoint(clientX, clientY)) return null;

    const stack = document.elementsFromPoint(clientX, clientY);
    const fromStack = stack.find((el) => el.classList?.contains("board-card"));
    if (fromStack) return fromStack;

    let best = null;
    let bestDist = Infinity;
    document.querySelectorAll(".board-card").forEach((card) => {
        if (card.classList.contains("ability-picked-up")) return;
        const r = card.getBoundingClientRect();
        if (
            clientX < r.left - tolerance
            || clientX > r.right + tolerance
            || clientY < r.top - tolerance
            || clientY > r.bottom + tolerance
        ) return;
        const cx = (r.left + r.right) / 2;
        const cy = (r.top + r.bottom) / 2;
        const dist = (cx - clientX) ** 2 + (cy - clientY) ** 2;
        if (dist < bestDist) {
            bestDist = dist;
            best = card;
        }
    });
    return best;
}

function activatePileControl(el) {
    if (!el) return false;
    if (el === els.drawBtn || el === els.drawPrompt) {
        tryDraw();
        return true;
    }
    if (el === els.takePrompt) {
        tryTake();
        return true;
    }
    if (el === els.playPrompt) {
        playDrawn();
        return true;
    }
    if (el === els.discardBtn) {
        onDiscardPileClick();
        return true;
    }
    return false;
}

function bindCardPointerFallback() {
    const stage = document.querySelector(".table-stage") || document.body;

    stage.addEventListener("click", (event) => {
        if (event.target.closest?.(".held-actions, .overlay-card, .btn:not(.pile-card):not(.prompt-label), a")) {
            return;
        }

        const pileEl = pileControlAtPoint(event.clientX, event.clientY);
        if (pileEl) {
            event.preventDefault();
            event.stopPropagation();
            activatePileControl(pileEl);
            return;
        }

        const touchTolerance = window.matchMedia?.("(pointer: coarse)")?.matches ? 10 : 0;
        const card = cardAtPoint(event.clientX, event.clientY, touchTolerance);
        if (!card || card.classList.contains("empty")) return;
        const owner = card.getAttribute("data-owner");
        const index = Number(card.getAttribute("data-index"));
        if (owner == null || !Number.isFinite(index)) return;
        event.preventDefault();
        event.stopPropagation();
        cardClicked(owner, index);
    }, true);

    stage.addEventListener("pointermove", (event) => {
        document.querySelectorAll(".board-card.is-hover").forEach((el) => el.classList.remove("is-hover"));
        if (pileControlAtPoint(event.clientX, event.clientY)) {
            stage.classList.add("card-cursor");
            return;
        }
        const card = cardAtPoint(event.clientX, event.clientY);
        if (card && !card.classList.contains("empty")) {
            card.classList.add("is-hover");
            stage.classList.add("card-cursor");
        } else {
            stage.classList.remove("card-cursor");
        }
    });

    stage.addEventListener("pointerleave", () => {
        document.querySelectorAll(".board-card.is-hover").forEach((el) => el.classList.remove("is-hover"));
        stage.classList.remove("card-cursor");
    });
}

function joinGame(username, options = {}) {
    myUsername = username;
    setRoomSessionValue("username", username);
    socket.emit("join", {
        room: ROOM_ID,
        username,
        reconnect_token: reconnectToken,
        bot_mode: Boolean(options.botMode),
        bot_count: options.botCount || 0,
        bot_difficulty: options.botDifficulty || "medium",
        bot_policy: options.botPolicy || null,
    });
}

function getRoomSessionValue(name) {
    try {
        return window.sessionStorage.getItem(`${roomStoragePrefix}${name}`) || "";
    } catch (_error) {
        return "";
    }
}

function setRoomSessionValue(name, value) {
    try {
        window.sessionStorage.setItem(`${roomStoragePrefix}${name}`, value);
    } catch (_error) {
        // Private browsing or locked-down storage should not block joining.
    }
}

function loadReconnectToken() {
    const saved = getRoomSessionValue("reconnect-token");
    if (saved) return saved;
    const token = window.crypto?.randomUUID
        ? window.crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
    setRoomSessionValue("reconnect-token", token);
    return token;
}

function savedRoomUsername() {
    return getRoomSessionValue("username");
}

function emptyRecentCardMarks() {
    return { looked: [], switched: [], createdBySid: "", throughTurnSid: "" };
}

function updateRecentCardMarks(action, actionKey, nextState) {
    const hasMarks = recentCardMarks.looked.length || recentCardMarks.switched.length;
    if (hasMarks) {
        const currentSid = nextState.current_turn_sid || "";
        if (!recentCardMarks.throughTurnSid && currentSid !== recentCardMarks.createdBySid) {
            recentCardMarks.throughTurnSid = currentSid;
        } else if (recentCardMarks.throughTurnSid && currentSid !== recentCardMarks.throughTurnSid) {
            recentCardMarks = emptyRecentCardMarks();
        }
    }
    if (nextState.status === "round_over" || nextState.status === "game_over") {
        recentCardMarks = emptyRecentCardMarks();
        return;
    }
    if (!action || !actionKey || actionKey === lastActionKey) return;
    if (action.type === "switch" && action.a && action.b) {
        recentCardMarks = {
            looked: [],
            switched: [action.a, action.b],
            createdBySid: action.sid || "",
            throughTurnSid: nextState.current_turn_sid !== action.sid
                ? nextState.current_turn_sid || ""
                : "",
        };
        return;
    }
    if (action.type === "peek" && action.owner_sid != null && action.index != null) {
        recentCardMarks = {
            looked: [{ owner_sid: action.owner_sid, index: action.index }],
            switched: [],
            createdBySid: action.sid || "",
            throughTurnSid: nextState.current_turn_sid !== action.sid
                ? nextState.current_turn_sid || ""
                : "",
        };
    }
}

function render(options = {}) {
    if (!state) return;
    if (state.status === "lobby") {
        keyboardNav.menuOpen = false;
        renderKeyboardActionMenu();
        if (els.lobby) els.lobby.classList.remove("hidden");
        els.game.classList.add("hidden");
        if (els.lobby) renderLobby();
        return;
    }

    if (els.lobby) els.lobby.classList.add("hidden");
    els.game.classList.remove("hidden");
    renderGame(options);
}

function renderLobby() {
    const amHost = state.host_sid === mySid;
    const humanCount = state.player_order.filter((sid) => !state.players[sid]?.is_bot).length;
    const botCount = state.player_order.filter((sid) => state.players[sid]?.is_bot).length;
    const botOnlyOpponents = humanCount === 1 && botCount > 0;
    els.hostControls.classList.toggle("read-only", !amHost);
    els.startBtn.classList.toggle("hidden", !amHost);
    els.addBotBtn.classList.toggle("hidden", !amHost || state.player_order.length >= 6);

    els.settingPreset.value = state.settings.preset || "custom";
    els.settingTarget.value = state.settings.target_score;
    els.settingWinCondition.value = state.settings.win_condition;
    els.settingGridRows.value = state.settings.grid_rows;
    els.settingGridCols.value = state.settings.grid_cols;
    els.settingJokerValue.value = state.settings.joker_value;
    els.settingJokers.value = state.settings.jokers ?? 2;
    els.settingDeckCount.value = state.settings.deck_count || 1;
    els.settingPeekDistance.value = state.settings.opponent_peek_distance;
    els.settingPeekDirection.value = state.settings.opponent_peek_direction;
    els.hostControls.querySelectorAll("input, select, button").forEach((control) => {
        control.disabled = !amHost;
    });
    renderPeekModeTools();
    renderGridRuleEditor();
    setLobbySettingsOpen(lobbySettingsOpen);

    els.playerList.innerHTML = state.player_order.map((sid) => {
        const player = state.players[sid];
        const autoReady = botOnlyOpponents && !player.is_bot;
        const readyText = player.ready || autoReady ? "READY" : "WAITING";
        const difficulty = player.difficulty || "medium";
        const botControls = player.is_bot && amHost
            ? `<div class="bot-row-controls">
                <select data-bot-difficulty="${escapeHtml(sid)}" aria-label="${escapeHtml(player.username)} difficulty">
                    ${["easy", "medium", "hard", "sweat", "custom"].map((level) => (
                        `<option value="${level}" ${difficulty === level ? "selected" : ""}>${level.toUpperCase()}</option>`
                    )).join("")}
                </select>
                <button type="button" class="bot-remove" data-remove-bot="${escapeHtml(sid)}" aria-label="Remove ${escapeHtml(player.username)}">×</button>
            </div>`
            : player.is_bot
                ? `<span class="bot-difficulty-label">${escapeHtml(difficulty.toUpperCase())}</span>`
                : `<b style="color:${player.ready || autoReady ? "#2ecc71" : "#e74c3c"}">${readyText}</b>`;
        return `
            <div class="lobby-player ${player.is_host ? "host" : ""}">
                <span class="lobby-player-name">${escapeHtml(player.username)} ${player.is_host ? "<em>HOST</em>" : ""} ${player.is_bot ? "<em>BOT</em>" : ""}</span>
                ${botControls}
            </div>
        `;
    }).join("");

    const me = state.players[mySid];
    if (me) {
        ready = me.ready;
        els.readyBtn.textContent = ready ? "NOT READY" : "READY UP";
        els.readyBtn.className = `btn ${ready ? "btn-red" : "btn-blue"}`;
        els.readyBtn.classList.toggle("hidden", botOnlyOpponents);
    }
}

function setLobbySettingsOpen(open) {
    lobbySettingsOpen = Boolean(open);
    els.hostControls?.classList.toggle("hidden", !lobbySettingsOpen);
    els.lobbyColumns?.classList.toggle("settings-open", lobbySettingsOpen);
    if (els.lobbySettingsToggle) {
        els.lobbySettingsToggle.setAttribute("aria-expanded", String(lobbySettingsOpen));
        els.lobbySettingsToggle.textContent = lobbySettingsOpen
            ? "CLOSE SETTINGS"
            : "ROOM SETTINGS";
    }
}

function renderPeekModeTools() {
    if (!els.peekModeTools) return;
    els.peekModeTools.querySelectorAll("[data-peek-mode]").forEach((button) => {
        button.classList.toggle("active", button.dataset.peekMode === activeGridPeekMode);
    });
}

function gridModeMeta(mode) {
    if (mode === "self") return { label: "OWN", symbol: "YOU", className: "self" };
    if (mode === "all_opponents") return { label: "ALL OPPONENTS", symbol: "ALL", className: "all" };
    if (mode === "seat_opponent") return { label: "CHOSEN OPPONENT", symbol: "SEAT", className: "chosen" };
    return { label: "NO OPENING PEEK", symbol: "—", className: "none" };
}

function renderGridRuleEditor() {
    if (!els.gridRuleEditor || !state) return;
    const rows = Number(state.settings.grid_rows);
    const cols = Number(state.settings.grid_cols);
    const modes = state.settings.grid_peek_modes || [];
    const amHost = state.host_sid === mySid;
    els.gridRuleEditor.style.gridTemplateColumns = `repeat(${cols}, minmax(54px, 72px))`;
    els.gridRuleEditor.innerHTML = Array.from({ length: rows * cols }, (_, index) => {
        const meta = gridModeMeta(modes[index]);
        return `
            <button type="button" class="grid-rule-cell ${meta.className}" data-grid-index="${index}" ${amHost ? "" : "disabled"} title="${meta.label}">
                <span class="grid-card-corner">${index + 1}</span>
                <strong>${meta.symbol}</strong>
                <small>${meta.label}</small>
            </button>
        `;
    }).join("");
}

function renderGame(options = {}) {
    normalizeKeyboardFocus();
    const current = state.players[state.current_turn_sid];
    const myTurn = state.current_turn_sid === mySid;
    els.turnText.textContent = myTurn ? "YOUR TURN" : `${current ? current.username.toUpperCase() : "WAITING"}'S TURN`;
    els.turnText.style.color = myTurn ? "#2ecc71" : "#f1c40f";
    els.phaseText.textContent = phaseText();

    renderScores();
    renderPiles(options);
    renderSeats(options);
    layoutArLabels();
    renderAbilityOverlay();
    renderBurnShowdown();
    renderHint();
    renderCallBtn();
    renderKeyboardActionMenu();
}

function phaseText() {
    if (state.status === "round_over") return "Round over";
    if (state.status === "game_over") return "Game over";
    if (state.phase === "final_countdown") {
        const seconds = Math.max(0, Math.ceil((finalCountdownEndsAt - Date.now()) / 1000));
        return seconds > 0 ? `Round ends in ${seconds}` : "Finishing round...";
    }
    if (state.first_caller_sid) {
        return `${state.players[state.first_caller_sid]?.username || "Someone"} called — final turns`;
    }
    if (state.held_peek?.sid === mySid) return "Peeking — put back or burn";
    if (state.phase === "drawn") return "Holding a card";
    if (state.phase === "ability") return "Special ability";
    return `Round ${state.round_number}`;
}

function updateCountdownText() {
    if (!state || state.phase !== "final_countdown" || !els.phaseText) return;
    els.phaseText.textContent = phaseText();
}

function renderScores() {
    if (!els.scoreChips) return;
    els.scoreChips.innerHTML = state.player_order.map((sid) => {
        const player = state.players[sid];
        const keyboardNumber = keyboardPlayerOrder().indexOf(sid) + 1;
        return `
            <div class="score-chip ${sid === mySid ? "me" : ""} ${player.eliminated ? "eliminated" : ""}" title="${escapeHtml(player.username)}">
                <span class="score-seat-label"><i>${keyboardNumber}</i><span class="score-name">${escapeHtml(player.username)}</span></span>
                <b>${player.score}${player.eliminated ? " · OUT" : ""}</b>
            </div>
        `;
    }).join("");
}

function renderCallBtn() {
    const canChoose = canChooseNow();
    els.callBtn.disabled = !canChoose || Boolean(state.pending_burn) || animating;
    els.callBtn.textContent = state.first_caller_sid ? "PROTECT" : "CALL";
}

function emitRoomSettings(overrides = {}) {
    if (!state || state.host_sid !== mySid || state.status !== "lobby") return;
    const settings = state.settings;
    socket.emit("update_settings", {
        room: ROOM_ID,
        preset: "custom",
        target_score: els.settingTarget?.value ?? settings.target_score,
        win_condition: els.settingWinCondition?.value ?? settings.win_condition,
        grid_rows: els.settingGridRows?.value ?? settings.grid_rows,
        grid_cols: els.settingGridCols?.value ?? settings.grid_cols,
        grid_peek_modes: overrides.grid_peek_modes || settings.grid_peek_modes,
        opponent_peek_distance: els.settingPeekDistance?.value ?? settings.opponent_peek_distance,
        opponent_peek_direction: els.settingPeekDirection?.value ?? settings.opponent_peek_direction,
        deck_count: els.settingDeckCount?.value ?? settings.deck_count,
        jokers: els.settingJokers?.value ?? settings.jokers,
        joker_value: els.settingJokerValue?.value ?? settings.joker_value,
        ...overrides,
    });
}

function resizeGridSettings() {
    if (!state) return;
    const oldRows = Number(state.settings.grid_rows);
    const oldCols = Number(state.settings.grid_cols);
    const newRows = Number(els.settingGridRows.value);
    const newCols = Number(els.settingGridCols.value);
    const modes = Array(newRows * newCols).fill("none");
    for (let row = 0; row < Math.min(oldRows, newRows); row += 1) {
        for (let col = 0; col < Math.min(oldCols, newCols); col += 1) {
            modes[row * newCols + col] = state.settings.grid_peek_modes[row * oldCols + col] || "none";
        }
    }
    state.settings.grid_rows = newRows;
    state.settings.grid_cols = newCols;
    state.settings.grid_peek_modes = modes;
    state.settings.preset = "custom";
    els.settingPreset.value = "custom";
    renderGridRuleEditor();
    emitRoomSettings({
        grid_rows: newRows,
        grid_cols: newCols,
        grid_peek_modes: modes,
    });
}

function canChooseNow() {
    return (
        state.status === "playing"
        && state.phase === "choose"
        && state.current_turn_sid === mySid
        && !state.players[mySid]?.called
        && !state.players[mySid]?.eliminated
        && !state.pending_burn
    );
}

function holdingMyDraw() {
    return state.pending_draw && state.pending_draw.sid === mySid && state.pending_draw.card;
}

function keyboardPlayerOrder() {
    if (!state?.players) return [];
    const order = (state.player_order || []).filter((sid) => state.players[sid]);
    if (!state.players[mySid]) return order;
    return [mySid, ...order.filter((sid) => sid !== mySid)];
}

function nonEmptyCardIndexes(ownerSid) {
    return (state?.players?.[ownerSid]?.board || [])
        .map((slot, index) => (!slot.empty ? index : -1))
        .filter((index) => index >= 0);
}

function normalizeKeyboardFocus() {
    if (!state?.players) return;
    const order = keyboardPlayerOrder();
    if (!order.length) {
        keyboardNav.ownerSid = "";
        keyboardNav.index = 0;
        return;
    }
    if (!order.includes(keyboardNav.ownerSid)) {
        keyboardNav.ownerSid = order[0];
    }
    let indexes = nonEmptyCardIndexes(keyboardNav.ownerSid);
    if (!indexes.length) {
        const nextOwner = order.find((sid) => nonEmptyCardIndexes(sid).length);
        if (!nextOwner) return;
        keyboardNav.ownerSid = nextOwner;
        indexes = nonEmptyCardIndexes(nextOwner);
    }
    if (!indexes.includes(keyboardNav.index)) keyboardNav.index = indexes[0];
}

function selectKeyboardPlayer(number) {
    const order = keyboardPlayerOrder();
    const ownerSid = order[number - 1];
    if (!ownerSid) {
        showToast(`No player is assigned to key ${number}.`);
        return;
    }
    const indexes = nonEmptyCardIndexes(ownerSid);
    if (!indexes.length) {
        showToast(`${state.players[ownerSid]?.username || "That player"} has no cards left.`);
        return;
    }
    keyboardNav.ownerSid = ownerSid;
    if (!indexes.includes(keyboardNav.index)) keyboardNav.index = indexes[0];
    refreshKeyboardFocusDom();
}

function moveKeyboardCard(dx, dy) {
    normalizeKeyboardFocus();
    const indexes = nonEmptyCardIndexes(keyboardNav.ownerSid);
    if (indexes.length < 2) return;
    const cols = Math.max(1, gridColsForBoard(state.players[keyboardNav.ownerSid].board.length));
    const currentRow = Math.floor(keyboardNav.index / cols);
    const currentCol = keyboardNav.index % cols;
    const candidates = indexes.map((index) => {
        const row = Math.floor(index / cols);
        const col = index % cols;
        const rowDelta = row - currentRow;
        const colDelta = col - currentCol;
        const forward = dx ? colDelta * dx : rowDelta * dy;
        return {
            index,
            forward,
            side: dx ? Math.abs(rowDelta) : Math.abs(colDelta),
        };
    }).filter((item) => item.forward > 0);
    if (!candidates.length) return;
    candidates.sort((a, b) => a.forward - b.forward || a.side - b.side || a.index - b.index);
    keyboardNav.index = candidates[0].index;
    refreshKeyboardFocusDom();
}

function refreshKeyboardFocusDom() {
    document.querySelectorAll(".board-card.keyboard-focus").forEach((card) => card.classList.remove("keyboard-focus"));
    document.querySelectorAll(".seat.keyboard-target, .nameplate.keyboard-target").forEach((item) => item.classList.remove("keyboard-target"));
    if (!keyboardNav.ownerSid) return;
    const owner = CSS.escape(keyboardNav.ownerSid);
    const card = document.querySelector(`.board-card[data-owner="${owner}"][data-index="${keyboardNav.index}"]`);
    if (card && !card.classList.contains("empty")) card.classList.add("keyboard-focus");
    document.querySelector(`.seat[data-sid="${owner}"]`)?.classList.add("keyboard-target");
    document.querySelector(`.nameplate[data-nameplate="${owner}"]`)?.classList.add("keyboard-target");
}

function keyboardMenuActions() {
    return [
        { id: "draw", label: "DRAW DECK", detail: "Pick up a hidden card", enabled: Boolean(state?.draw_count) },
        { id: "take", label: "TAKE DISCARD", detail: "Must swap into your grid", enabled: Boolean(state?.discard_top) },
        { id: "call", label: "CALL", detail: "End your turn and begin the finish", enabled: true },
    ];
}

function normalizeKeyboardMenuIndex(actions = keyboardMenuActions()) {
    if (actions[keyboardNav.menuIndex]?.enabled) return;
    const firstEnabled = actions.findIndex((action) => action.enabled);
    keyboardNav.menuIndex = Math.max(0, firstEnabled);
}

function renderKeyboardActionMenu() {
    if (!els.keyboardActionMenu) return;
    if (!keyboardNav.menuOpen || !canChooseNow()) {
        keyboardNav.menuOpen = false;
        els.keyboardActionMenu.classList.add("hidden");
        els.keyboardActionMenu.innerHTML = "";
        return;
    }
    const actions = keyboardMenuActions();
    normalizeKeyboardMenuIndex(actions);
    els.keyboardActionMenu.innerHTML = `
        <div class="keyboard-menu-title"><span>ENTER</span> CHOOSE YOUR MOVE</div>
        <div class="keyboard-menu-options">
            ${actions.map((action, index) => `
                <button type="button" class="keyboard-menu-option${index === keyboardNav.menuIndex ? " selected" : ""}" data-keyboard-action="${action.id}" ${action.enabled ? "" : "disabled"}>
                    <strong>${action.label}</strong><small>${action.detail}</small>
                </button>
            `).join("")}
        </div>
        <p>Arrow keys / WASD to move · Space to confirm · Esc to close</p>
    `;
    els.keyboardActionMenu.classList.remove("hidden");
}

function moveKeyboardMenu(delta) {
    const actions = keyboardMenuActions();
    const enabled = actions.map((action, index) => (action.enabled ? index : -1)).filter((index) => index >= 0);
    if (!enabled.length) return;
    const current = enabled.indexOf(keyboardNav.menuIndex);
    const offset = current < 0 ? 0 : current;
    keyboardNav.menuIndex = enabled[(offset + delta + enabled.length) % enabled.length];
    renderKeyboardActionMenu();
}

function confirmKeyboardMenuAction() {
    if (!keyboardNav.menuOpen || !canChooseNow() || animating) return;
    const action = keyboardMenuActions()[keyboardNav.menuIndex];
    if (!action?.enabled) return;
    keyboardNav.menuOpen = false;
    renderKeyboardActionMenu();
    if (action.id === "draw") tryDraw();
    else if (action.id === "take") tryTake();
    else if (action.id === "call") socket.emit("call_round", { room: ROOM_ID });
}

function openKeybinds() {
    keyboardNav.menuOpen = false;
    renderKeyboardActionMenu();
    els.keybindsOverlay?.classList.remove("hidden");
    els.keybindsClose?.focus();
}

function closeKeybinds() {
    els.keybindsOverlay?.classList.add("hidden");
    els.keybindsBtn?.focus();
}

function gameShortcutBlocked(target) {
    return Boolean(target?.closest?.("input, textarea, select, button, a, [contenteditable='true']"));
}

function handleGameKeydown(event) {
    const key = event.key.toLowerCase();
    if (!els.keybindsOverlay?.classList.contains("hidden")) {
        if (event.key === "Escape" || key === "k" || event.key === "?") {
            event.preventDefault();
            closeKeybinds();
        }
        return;
    }
    if (gameShortcutBlocked(event.target)) return;
    if (key === "k" || event.key === "?") {
        event.preventDefault();
        openKeybinds();
        return;
    }
    if (!state || state.status !== "playing" || !els.lobby?.classList.contains("hidden")) return;

    if (event.key === "Escape") {
        if (keyboardNav.menuOpen) {
            event.preventDefault();
            keyboardNav.menuOpen = false;
            renderKeyboardActionMenu();
        }
        return;
    }
    if (/^[1-6]$/.test(event.key)) {
        event.preventDefault();
        selectKeyboardPlayer(Number(event.key));
        return;
    }

    const direction = {
        ArrowLeft: [-1, 0], a: [-1, 0],
        ArrowRight: [1, 0], d: [1, 0],
        ArrowUp: [0, -1], w: [0, -1],
        ArrowDown: [0, 1], s: [0, 1],
    }[event.key] || ({ a: [-1, 0], d: [1, 0], w: [0, -1], s: [0, 1] })[key];
    if (direction) {
        event.preventDefault();
        if (keyboardNav.menuOpen) moveKeyboardMenu(direction[0] < 0 || direction[1] < 0 ? -1 : 1);
        else moveKeyboardCard(direction[0], direction[1]);
        return;
    }
    if (event.code === "Space") {
        event.preventDefault();
        if (keyboardNav.menuOpen) {
            confirmKeyboardMenuAction();
            return;
        }
        normalizeKeyboardFocus();
        if (!keyboardNav.ownerSid) return;
        cardClicked(keyboardNav.ownerSid, keyboardNav.index);
        return;
    }
    if (event.key === "Enter") {
        event.preventDefault();
        if (animating) return;
        if (keyboardNav.menuOpen) {
            confirmKeyboardMenuAction();
        } else if (holdingMyDraw() && state.pending_draw.source === "draw") {
            playDrawn();
        } else if (holdingMyDraw()) {
            showToast("A discard pickup must be switched into your grid. Choose a card and press Space.");
        } else if (canChooseNow()) {
            keyboardNav.menuOpen = true;
            normalizeKeyboardMenuIndex();
            renderKeyboardActionMenu();
        } else {
            showToast("The action menu opens at the start of your turn.");
        }
    }
}

function playerIsHolding(sid) {
    return Boolean(state.pending_draw && state.pending_draw.sid === sid);
}

function renderPiles(options = {}) {
    const keepPreviousDiscard = Boolean(
        options.hideAnimTargets
        && prevState
        && (
            options.action?.type === "swap"
            || options.action?.type === "play"
            || options.action?.type === "burn"
        )
    );
    const keepPreviousDraw = Boolean(
        options.hideAnimTargets
        && prevState
        && options.action?.type === "burn_fail"
        && options.action?.penalty
    );
    const visibleDiscard = keepPreviousDiscard ? prevState.discard_top : state.discard_top;
    const visibleDiscardCount = keepPreviousDiscard ? prevState.discard_count : state.discard_count;
    const visibleDrawCount = keepPreviousDraw ? prevState.draw_count : state.draw_count;

    els.drawCount.textContent = String(visibleDrawCount);
    els.discardCount.textContent = String(visibleDiscardCount);

    const choose = canChooseNow();
    const holding = holdingMyDraw();
    const canPlay = holding && state.pending_draw.source === "draw";

    els.drawPrompt.classList.toggle("hidden", !choose || state.draw_count === 0);
    els.takePrompt.classList.toggle("hidden", !choose || !state.discard_top);
    els.playPrompt.classList.toggle("hidden", !canPlay);

    els.drawBtn.disabled = !choose || state.draw_count === 0;
    els.discardBtn.disabled = !(choose && state.discard_top) && !canPlay;

    if (visibleDiscard) {
        els.discardBtn.className = `playing-card pile-card ${colorClass(visibleDiscard)}`;
        els.discardBtn.innerHTML = cardFaceHtml(visibleDiscard);
    } else {
        els.discardBtn.className = "playing-card pile-card empty-card";
        els.discardBtn.textContent = "EMPTY";
    }

    requestAnimationFrame(layoutArLabels);
}

/** Place draw/take/play as screen-flat HUD text just above each pile card. */
function layoutArLabels() {
    const stage = document.querySelector(".table-stage");
    const overlay = document.querySelector(".ar-labels");
    if (!stage || !overlay || !els.drawPrompt) return;
    const stageRect = stage.getBoundingClientRect();
    const overlayRect = overlay.getBoundingClientRect();
    if (stageRect.width < 8) return;

    const placeAbove = (btn, cardEl, xOffset = 0) => {
        if (!btn || !cardEl) return;
        if (btn.classList.contains("hidden")) {
            btn.classList.remove("is-placed");
            return;
        }
        const r = cardEl.getBoundingClientRect();
        const cx = r.left + r.width / 2 + xOffset;
        const top = r.top - 8;
        btn.style.left = `${(cx - overlayRect.left).toFixed(2)}px`;
        btn.style.top = `${(top - overlayRect.top).toFixed(2)}px`;
        btn.classList.add("is-placed");
    };

    placeAbove(els.drawPrompt, els.drawBtn);

    const takeHidden = !els.takePrompt || els.takePrompt.classList.contains("hidden");
    const playHidden = !els.playPrompt || els.playPrompt.classList.contains("hidden");
    if (!takeHidden && !playHidden) {
        placeAbove(els.takePrompt, els.discardBtn, -36);
        placeAbove(els.playPrompt, els.discardBtn, 36);
    } else {
        placeAbove(els.takePrompt, els.discardBtn);
        placeAbove(els.playPrompt, els.discardBtn);
    }
}

function tryDraw() {
    if (animating) return;
    if (!state || state.status !== "playing") return;
    if (state.phase !== "choose" || state.current_turn_sid !== mySid) {
        showToast("Wait for your turn to draw.");
        return;
    }
    if (state.pending_burn) {
        showToast("Finish the burn first.");
        return;
    }
    if (state.draw_count === 0) {
        showToast("Draw pile is empty.");
        return;
    }
    socket.emit("draw_from_deck", { room: ROOM_ID });
}

function tryTake() {
    if (animating) return;
    if (!canChooseNow() || !state.discard_top) return;
    socket.emit("take_discard", { room: ROOM_ID });
}

function playDrawn() {
    if (!holdingMyDraw() || state.pending_draw.source !== "draw") return;
    socket.emit("play_drawn", { room: ROOM_ID });
}

function onDiscardPileClick() {
    if (holdingMyDraw() && state.pending_draw.source === "draw") {
        playDrawn();
        return;
    }
    tryTake();
}

function rotatedOrder() {
    const order = state.player_order.slice();
    const myIndex = order.indexOf(mySid);
    if (myIndex < 0) return order;
    return order.slice(myIndex).concat(order.slice(0, myIndex));
}


/**
 * Table camera / seat layout.
 * CSS owns perspective + rotateX(15deg). Seats live inside that plane, so
 * cards and their shadows are coplanar with the felt. Labels/nameplates are
 * positioned later in screen space.
 */
const TABLE = {
    width: 12,
    depth: 7,
    cameraDistance: 7,
    cameraHeight: 13,
    cardWidth: 0.77,
    cardHeight: 1.078,
    cardGap: 0.09,
    heldCardGap: 0.43,
    heldCardTilt: -74,
    pileY: -0.20,
    rimFraction: 0.95,
    centerClearanceX: 1.40,
    centerClearanceY: 0.86,
    maxBoardWidth: 3.20,
    maxBoardHeight: 2.72,
    minSeatScale: 0.12,
    ringByCount: {
        2: 0.70,
        3: 0.72,
        4: 0.73,
        5: 0.74,
        6: 0.75,
    },
};

TABLE.cameraLength = Math.hypot(TABLE.cameraDistance, TABLE.cameraHeight);
TABLE.pitch = Math.atan2(TABLE.cameraDistance, TABLE.cameraHeight);

/** Azimuth rings for placement only (0 = local/near). Yaw is computed separately. */
const SEAT_AZIMUTHS = {
    2: [0, 180],
    3: [0, 118, -118],
    4: [0, 90, 180, -90],
    5: [0, 58, 138, -138, -58],
    6: [0, 48, 110, 180, -110, -48],
};

function shortestDeg(deg) {
    return ((((deg + 180) % 360) + 360) % 360) - 180;
}

function applyTableCameraSettings(force = false) {
    const table3d = document.querySelector(".table-3d");
    const stage = document.querySelector(".table-stage");
    if (!table3d || !stage) return null;

    const measuredWidth = stage.clientWidth;
    const measuredHeight = stage.clientHeight;
    if (measuredWidth < 8 || measuredHeight < 8) return null;
    if (!force && tableCameraCache) return tableCameraCache;
    const compactLandscape = window.matchMedia?.(
        "(max-width: 850px) and (orientation: landscape)",
    )?.matches;
    // A phone browser can leave less than 250px after its landscape toolbars.
    // Never size the table against an invented minimum or the local/bottom row
    // will be rendered below the clipped play area.
    const stageWidth = compactLandscape ? measuredWidth : Math.max(320, measuredWidth);
    const stageHeight = compactLandscape ? measuredHeight : Math.max(420, measuredHeight);
    const topClearance = compactLandscape ? 6 : stageWidth < 700 ? 34 : 42;
    const legReserve = compactLandscape ? 10 : stageWidth < 700 ? 74 : 122;
    const l = TABLE.cameraLength;
    const d = TABLE.cameraDistance;
    const h = TABLE.cameraHeight;
    const b = TABLE.depth / 2;
    const nearPerUnit = h * l * b / (l * l - d * b);
    const farPerUnit = h * l * b / (l * l + d * b);
    const unitByWidth = stageWidth * 0.92 / TABLE.width;
    const usableHeight = Math.max(1, stageHeight - topClearance - legReserve);
    const unitByHeight = usableHeight / (nearPerUnit + farPerUnit);
    const scale = Math.max(compactLandscape ? 8 : 24, Math.min(110, unitByWidth, unitByHeight));
    const centerY = topClearance + farPerUnit * scale;
    const cardWidth = TABLE.cardWidth * scale;
    const cardHeight = TABLE.cardHeight * scale;
    const gap = TABLE.cardGap * scale;

    const vars = {
        "--world-scale": `${scale.toFixed(3)}px`,
        "--camera-focal": `${(scale * l).toFixed(3)}px`,
        "--camera-pitch": `${(TABLE.pitch * 180 / Math.PI).toFixed(4)}deg`,
        "--table-center-y": `${centerY.toFixed(3)}px`,
        "--table-world-width": `${(TABLE.width * scale).toFixed(3)}px`,
        "--table-world-depth": `${(TABLE.depth * scale).toFixed(3)}px`,
        "--card-width": `${cardWidth.toFixed(3)}px`,
        "--card-height": `${cardHeight.toFixed(3)}px`,
        "--card-gap": `${gap.toFixed(3)}px`,
        "--grid-width-2": `${(cardWidth * 2 + gap).toFixed(3)}px`,
        "--grid-width-3": `${(cardWidth * 3 + gap * 2).toFixed(3)}px`,
        "--grid-width-4": `${(cardWidth * 4 + gap * 3).toFixed(3)}px`,
        "--pile-world-y": `${(TABLE.pileY * scale).toFixed(3)}px`,
    };
    const handLayer = document.querySelector(".hand-overlays");
    Object.entries(vars).forEach(([name, value]) => {
        table3d.style.setProperty(name, value);
        handLayer?.style.setProperty(name, value);
    });
    table3d.dataset.worldScale = scale.toFixed(4);
    table3d.dataset.cameraLength = l.toFixed(4);
    table3d.dataset.projectedNearFarRatio = ((l * l + d * b) / (l * l - d * b)).toFixed(4);
    requestAnimationFrame(layoutTableLegs);
    tableCameraCache = { scale, centerY };
    return tableCameraCache;
}

function projectWorldPoint(x, y) {
    const table3d = document.querySelector(".table-3d");
    const stage = document.querySelector(".table-stage");
    if (!table3d || !stage) return null;
    const scale = Number(table3d?.dataset.worldScale);
    const centerY = parseFloat(getComputedStyle(table3d).getPropertyValue("--table-center-y"));
    if (!scale || !Number.isFinite(centerY)) return null;
    const focal = scale * TABLE.cameraLength;
    const denominator = focal - scale * y * Math.sin(TABLE.pitch);
    return {
        x: stage.clientWidth / 2 + focal * scale * x / denominator,
        y: centerY + focal * scale * y * Math.cos(TABLE.pitch) / denominator,
    };
}

/**
 * CSS rotateZ whose local "top" points from the seat toward table center.
 * Uses clientWidth/Height (unprojected), never getBoundingClientRect().
 */
function gridColsForBoard(boardLength) {
    const configured = Number(state?.settings?.grid_cols);
    if (configured >= 2 && configured <= 4) return configured;
    return boardLength <= 4 ? 2 : Math.min(4, Math.ceil(Math.sqrt(boardLength)));
}

function seatDensityScale(count) {
    if (count >= 6) return 0.80;
    if (count >= 5) return 0.84;
    if (count >= 4) return 0.91;
    return 1;
}

function boardShape(sid) {
    const player = state?.players?.[sid];
    const boardLength = player?.board?.length || 4;
    const cols = gridColsForBoard(boardLength);
    const rows = Math.max(1, Math.ceil(boardLength / cols));
    const boardWidth = cols * TABLE.cardWidth + (cols - 1) * TABLE.cardGap;
    const boardHeight = rows * TABLE.cardHeight + (rows - 1) * TABLE.cardGap;
    return { boardLength, cols, rows, boardWidth, boardHeight };
}

function heldPlacementFor(sid) {
    const shape = boardShape(sid);
    const crowdedTable = (state?.player_order?.length || 0) >= 5;
    const narrowScreen = window.innerWidth < 760;
    return shape.cols >= 4 || crowdedTable || narrowScreen ? "below" : "side";
}

function boardGrowthScale(sid) {
    const shape = boardShape(sid);
    return Math.max(
        TABLE.minSeatScale,
        Math.min(
            1,
            TABLE.maxBoardWidth / shape.boardWidth,
            TABLE.maxBoardHeight / shape.boardHeight,
        ),
    );
}

function boardFootprintWorld(sid, scale) {
    const shape = boardShape(sid);
    return {
        xMin: (-shape.boardWidth / 2) * scale,
        xMax: (shape.boardWidth / 2) * scale,
        yMin: (-shape.boardHeight / 2) * scale,
        yMax: (shape.boardHeight / 2) * scale,
    };
}

function seatPoseAt(phi, ring) {
    const radial = {
        x: TABLE.width / 2 * Math.cos(phi),
        y: TABLE.depth / 2 * Math.sin(phi),
    };
    const center = { x: radial.x * ring, y: radial.y * ring };
    const length = Math.hypot(radial.x, radial.y) || 1;
    return { center, outward: { x: radial.x / length, y: radial.y / length } };
}

/** Density scale by player count only — never by camera depth. */
function footprintCorners(center, outward, footprint) {
    const tangent = { x: outward.y, y: -outward.x };
    return [
        [footprint.xMin, footprint.yMin],
        [footprint.xMax, footprint.yMin],
        [footprint.xMax, footprint.yMax],
        [footprint.xMin, footprint.yMax],
    ].map(([lx, ly]) => ({
            x: center.x + tangent.x * lx + outward.x * ly,
            y: center.y + tangent.y * lx + outward.y * ly,
        }));
}

function footprintInsideEllipse(center, outward, footprint) {
    const a = TABLE.width / 2 * TABLE.rimFraction;
    const b = TABLE.depth / 2 * TABLE.rimFraction;
    return footprintCorners(center, outward, footprint).every((point) => (
        (point.x / a) ** 2 + (point.y / b) ** 2 <= 1
    ));
}

function pointInsidePolygon(point, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
        const a = polygon[i];
        const b = polygon[j];
        const crosses = ((a.y > point.y) !== (b.y > point.y))
            && point.x < ((b.x - a.x) * (point.y - a.y)) / ((b.y - a.y) || 1e-9) + a.x;
        if (crosses) inside = !inside;
    }
    return inside;
}

function distanceToSegment(point, a, b) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const lengthSq = dx * dx + dy * dy;
    if (lengthSq <= 1e-12) return Math.hypot(point.x - a.x, point.y - a.y);
    const t = Math.max(
        0,
        Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / lengthSq),
    );
    return Math.hypot(point.x - (a.x + dx * t), point.y - (a.y + dy * t));
}

function footprintOutsideCenter(center, outward, footprint) {
    const normalized = footprintCorners(center, outward, footprint).map((point) => ({
        x: point.x / TABLE.centerClearanceX,
        y: (point.y - TABLE.pileY) / TABLE.centerClearanceY,
    }));
    if (pointInsidePolygon({ x: 0, y: 0 }, normalized)) return false;
    let distance = Infinity;
    for (let i = 0; i < normalized.length; i += 1) {
        distance = Math.min(
            distance,
            distanceToSegment(
                { x: 0, y: 0 },
                normalized[i],
                normalized[(i + 1) % normalized.length],
            ),
        );
    }
    return distance >= 1;
}

function outerRingLimit(phi, footprint) {
    const centerPose = seatPoseAt(phi, 0);
    if (!footprintInsideEllipse(centerPose.center, centerPose.outward, footprint)) return null;
    const edgePose = seatPoseAt(phi, 1);
    if (footprintInsideEllipse(edgePose.center, edgePose.outward, footprint)) return 1;
    let low = 0;
    let high = 1;
    for (let i = 0; i < 24; i += 1) {
        const mid = (low + high) / 2;
        const pose = seatPoseAt(phi, mid);
        if (footprintInsideEllipse(pose.center, pose.outward, footprint)) low = mid;
        else high = mid;
    }
    return low;
}

function innerRingLimit(phi, outerLimit, footprint) {
    const centerPose = seatPoseAt(phi, 0);
    if (footprintOutsideCenter(centerPose.center, centerPose.outward, footprint)) return 0;
    const outerPose = seatPoseAt(phi, outerLimit);
    if (!footprintOutsideCenter(outerPose.center, outerPose.outward, footprint)) return null;
    let low = 0;
    let high = outerLimit;
    for (let i = 0; i < 24; i += 1) {
        const mid = (low + high) / 2;
        const pose = seatPoseAt(phi, mid);
        if (footprintOutsideCenter(pose.center, pose.outward, footprint)) high = mid;
        else low = mid;
    }
    return high;
}

function solveSeatPlacement(phi, requestedRing, requestedScale, sid) {
    let scale = Math.max(TABLE.minSeatScale, requestedScale);
    for (let attempt = 0; attempt < 30; attempt += 1) {
        const footprint = boardFootprintWorld(sid, scale);
        const outerLimit = outerRingLimit(phi, footprint);
        if (outerLimit != null) {
            const innerLimit = innerRingLimit(phi, outerLimit, footprint);
            if (innerLimit != null && innerLimit <= outerLimit) {
                const ring = Math.max(innerLimit, Math.min(requestedRing, outerLimit));
                return {
                    scale,
                    ring,
                    footprint,
                    ...seatPoseAt(phi, ring),
                };
            }
        }
        scale = Math.max(TABLE.minSeatScale, scale - 0.025);
    }
    const footprint = boardFootprintWorld(sid, TABLE.minSeatScale);
    const outerLimit = outerRingLimit(phi, footprint);
    const ring = Math.min(requestedRing, outerLimit == null ? requestedRing : outerLimit);
    return {
        scale: TABLE.minSeatScale,
        ring,
        footprint,
        ...seatPoseAt(phi, ring),
    };
}

function polygonAxes(polygon) {
    return polygon.map((point, index) => {
        const next = polygon[(index + 1) % polygon.length];
        const dx = next.x - point.x;
        const dy = next.y - point.y;
        const length = Math.hypot(dx, dy) || 1;
        return { x: -dy / length, y: dx / length };
    });
}

function polygonsOverlap(first, second, padding = 0.06) {
    return [...polygonAxes(first), ...polygonAxes(second)].every((axis) => {
        const project = (polygon) => polygon.map((point) => point.x * axis.x + point.y * axis.y);
        const a = project(first);
        const b = project(second);
        return Math.max(...a) + padding >= Math.min(...b)
            && Math.max(...b) + padding >= Math.min(...a);
    });
}

function seatLayout(order) {
    const n = Math.max(2, Math.min(6, order.length || 2));
    const azList = SEAT_AZIMUTHS[n] || SEAT_AZIMUTHS[4];
    const baseRing = TABLE.ringByCount[n] || 0.68;

    const specs = azList.map((azimuthDeg, i) => {
        const sid = order[i];
        return {
            sid,
            isLocal: i === 0,
            phi: ((90 + azimuthDeg) * Math.PI) / 180,
            scaleCap: seatDensityScale(n) * boardGrowthScale(sid),
        };
    });

    let placements = [];
    for (let pass = 0; pass < 18; pass += 1) {
        placements = specs.map((spec) => (
            solveSeatPlacement(spec.phi, baseRing, spec.scaleCap, spec.sid)
        ));
        const collisions = new Set();
        placements.forEach((placement, index) => {
            const first = footprintCorners(
                placement.center,
                placement.outward,
                placement.footprint,
            );
            placements.slice(index + 1).forEach((other, offset) => {
                const secondIndex = index + offset + 1;
                const second = footprintCorners(
                    other.center,
                    other.outward,
                    other.footprint,
                );
                if (polygonsOverlap(first, second)) {
                    collisions.add(index);
                    collisions.add(secondIndex);
                }
            });
        });
        if (!collisions.size) break;
        collisions.forEach((index) => {
            specs[index].scaleCap = Math.max(
                TABLE.minSeatScale,
                placements[index].scale * 0.93,
            );
        });
    }

    const equalBoardGroups = new Map();
    specs.forEach((spec, index) => {
        const length = boardShape(spec.sid).boardLength;
        if (!equalBoardGroups.has(length)) equalBoardGroups.set(length, []);
        equalBoardGroups.get(length).push(index);
    });
    let normalizedScale = false;
    equalBoardGroups.forEach((indices) => {
        if (indices.length < 2) return;
        const sharedScale = Math.min(...indices.map((index) => placements[index].scale));
        indices.forEach((index) => {
            if (specs[index].scaleCap > sharedScale) {
                specs[index].scaleCap = sharedScale;
                normalizedScale = true;
            }
        });
    });
    if (normalizedScale) {
        placements = specs.map((spec) => (
            solveSeatPlacement(spec.phi, baseRing, spec.scaleCap, spec.sid)
        ));
    }

    return specs.map((spec, index) => {
        const { isLocal, phi } = spec;
        const placement = placements[index];
        const { scale, ring } = placement;
        const pose = placement;
        const left = 50 + pose.center.x / TABLE.width * 100;
        const top = 50 + pose.center.y / TABLE.depth * 100;

        // Yaw only after final position — matches real W/H pixels.
        const yaw = Math.atan2(-pose.outward.x, pose.outward.y) * 180 / Math.PI;

        return {
            left,
            top,
            yaw,
            scale,
            isLocal,
            ring,
            worldX: pose.center.x,
            worldY: pose.center.y,
            phi,
        };
    });
}

function renderSeats(options = {}) {
    applyTableCameraSettings();
    const order = rotatedOrder();
    const layoutKey = order.map((sid) => (
        `${sid}:${state.players[sid]?.board?.length || 0}`
    )).join("|");
    if (seatLayoutCache.key !== layoutKey) {
        seatLayoutCache = { key: layoutKey, positions: seatLayout(order) };
    }
    const positions = seatLayoutCache.positions;
    els.seats.innerHTML = order
        .map((sid, index) => renderSeat(sid, positions[index], options))
        .join("");
    els.seats.dataset.count = String(order.length);
    renderHands(order, options);
    renderNameplates(order, positions);
    requestAnimationFrame(() => {
        layoutNameplates(order);
        layoutHands(order);
        layoutTableLegs();
    });
}

function renderHands(order, options = {}) {
    if (!els.hands) return;
    els.hands.innerHTML = order.filter((sid) => (
        !state.players[sid]?.is_bot && !state.players[sid]?.eliminated
    )).map((sid) => {
        const hideHeld = options.hideAnimTargets && options.action
            && (options.action.type === "draw" || options.action.type === "take")
            && options.action.sid === sid;
        return renderHeldSlot(sid, { hidden: hideHeld });
    }).join("");
    layoutHands(order);
}

function layoutHands(order) {
    if (!els.hands) return;
    const stage = document.querySelector(".table-stage");
    if (!stage) return;
    const stageRect = stage.getBoundingClientRect();
    const layerRect = els.hands.getBoundingClientRect();
    order.forEach((sid) => {
        if (state.players[sid]?.is_bot) return;
        const hand = els.hands.querySelector(`[data-held="${CSS.escape(sid)}"]`);
        const frame = seatWorldFrame(sid);
        const anchor = heldWorldAnchor(sid);
        if (!hand || !frame || !anchor) return;
        const point = projectWorldPoint(anchor.x, anchor.y);
        if (!point) return;
        const depthScale = TABLE.cameraLength ** 2
            / (TABLE.cameraLength ** 2 - TABLE.cameraDistance * anchor.y);
        let left = stageRect.left - layerRect.left + point.x;
        let top = stageRect.top - layerRect.top + point.y;
        hand.style.left = `${left.toFixed(2)}px`;
        hand.style.top = `${top.toFixed(2)}px`;
        const handYaw = state.players[sid]?.is_bot ? 0 : uprightHandYaw(frame.yaw);
        hand.style.setProperty("--hand-yaw", `${handYaw.toFixed(2)}deg`);
        hand.style.setProperty("--hand-depth-scale", depthScale.toFixed(4));
    });
}

function renderNameplates(order, positions) {
    if (!els.nameplates) return;
    els.nameplates.innerHTML = order.map((sid, i) => {
        const player = state.players[sid];
        const isMe = sid === mySid;
        const pos = positions[i];
        // Rough radial seed so plates never flash at center before layout.
        const left = 50 + (pos.left - 50) * 1.42;
        const top = 50 + (pos.top - 50) * 1.28;
        const classes = ["nameplate"];
        if (isMe) classes.push("me");
        if (player.protected) classes.push("protected");
        if (sid === state.current_turn_sid) classes.push("current-turn");
        if (sid === keyboardNav.ownerSid) classes.push("keyboard-target");
        const keyboardNumber = keyboardPlayerOrder().indexOf(sid) + 1;
        const badges = [
            keyboardNumber > 0 ? `<span class="badge key-badge">${keyboardNumber}</span>` : "",
            player.called ? `<span class="badge yellow">CALLED</span>` : "",
            player.protected && !player.called ? `<span class="badge yellow">SAFE</span>` : "",
            `<span class="badge blue turn-badge${sid === state.current_turn_sid ? "" : " is-off"}">TURN</span>`,
            player.is_bot ? `<span class="badge green">BOT</span>` : "",
            player.eliminated ? `<span class="badge red">SPECTATING</span>` : "",
            !player.connected ? `<span class="badge">OFF</span>` : "",
        ].join("");
        return `
            <div class="${classes.join(" ")}" data-nameplate="${sid}" data-seat-phi="${pos.phi.toFixed(6)}" style="left:${left.toFixed(2)}%;top:${top.toFixed(2)}%">
                <span class="seat-name">${escapeHtml(player.username)}${isMe ? " (you)" : ""}</span>
                <div class="seat-badges">${badges}</div>
            </div>
        `;
    }).join("");

    layoutNameplates(order);
}

/** Snap each nameplate onto the radial line from table center, entirely outside the oval. */
function layoutNameplates(order) {
    const stage = document.querySelector(".table-stage");
    if (!stage || !els.nameplates) return;
    const stageRect = stage.getBoundingClientRect();
    const plateBox = els.nameplates.getBoundingClientRect();
    if (stageRect.width < 8 || stageRect.height < 8 || plateBox.width < 8) return;
    const projectedCenter = projectWorldPoint(0, 0);
    if (!projectedCenter) return;

    order.forEach((sid) => {
        const plate = els.nameplates.querySelector(`[data-nameplate="${CSS.escape(sid)}"]`);
        if (!plate) return;
        const phi = Number(plate.dataset.seatPhi);
        const edge = projectWorldPoint(
            TABLE.width / 2 * Math.cos(phi),
            TABLE.depth / 2 * Math.sin(phi),
        );
        if (!edge) return;
        let dx = edge.x - projectedCenter.x;
        let dy = edge.y - projectedCenter.y;
        const len = Math.hypot(dx, dy) || 1;
        dx /= len;
        dy /= len;
        const platePad = Math.max(plate.offsetWidth, plate.offsetHeight) * 0.55 + 18;
        const desiredX = stageRect.left + edge.x + dx * platePad;
        const desiredY = stageRect.top + edge.y + dy * platePad;
        const halfWidth = plate.offsetWidth / 2;
        const halfHeight = plate.offsetHeight / 2;
        const x = Math.max(
            stageRect.left + halfWidth + 6,
            Math.min(stageRect.right - halfWidth - 6, desiredX),
        );
        const y = Math.max(
            stageRect.top + halfHeight + 6,
            Math.min(stageRect.bottom - halfHeight - 6, desiredY),
        );
        plate.style.left = `${(((x - plateBox.left) / plateBox.width) * 100).toFixed(2)}%`;
        plate.style.top = `${(((y - plateBox.top) / plateBox.height) * 100).toFixed(2)}%`;
    });
}

/** Legs are screen-space furniture attached to the projected lower ellipse. */
function layoutTableLegs() {
    const stage = document.querySelector(".table-stage");
    const table3d = document.querySelector(".table-3d");
    const legs = document.querySelector(".table-legs");
    if (!stage || !table3d || !legs) return;
    const stageRect = stage.getBoundingClientRect();
    const layerRect = legs.getBoundingClientRect();
    const scale = Number(table3d.dataset.worldScale);
    const centerY = parseFloat(getComputedStyle(table3d).getPropertyValue("--table-center-y"));
    if (!scale || !centerY) return;

    const focal = scale * TABLE.cameraLength;
    const sinPitch = Math.sin(TABLE.pitch);
    const cosPitch = Math.cos(TABLE.pitch);
    const normalizedX = 0.22;
    const worldX = TABLE.width / 2 * normalizedX;
    const worldY = TABLE.depth / 2 * Math.sqrt(1 - normalizedX ** 2);
    const denominator = focal - scale * worldY * sinPitch;
    const projectX = (x) => stageRect.width / 2 + focal * scale * x / denominator;
    const attachY = centerY + focal * scale * worldY * cosPitch / denominator - 6;
    const projectedWidth = focal * scale * TABLE.width / focal;
    const compact = stageRect.width < 700;
    const width = Math.max(compact ? 24 : 58, Math.min(108, projectedWidth * 0.075));
    const availableHeight = Math.max(64, stageRect.height - attachY - 8);
    const height = Math.min(Math.max(compact ? 46 : 112, scale * 1.55), availableHeight);

    const stageOffsetX = stageRect.left - layerRect.left;
    const stageOffsetY = stageRect.top - layerRect.top;
    legs.style.setProperty("--leg-left-x", `${(stageOffsetX + projectX(-worldX)).toFixed(2)}px`);
    legs.style.setProperty("--leg-right-x", `${(stageOffsetX + projectX(worldX)).toFixed(2)}px`);
    legs.style.setProperty("--leg-top", `${(stageOffsetY + attachY).toFixed(2)}px`);
    legs.style.setProperty("--leg-width", `${width.toFixed(2)}px`);
    legs.style.setProperty("--leg-height", `${height.toFixed(2)}px`);
}

function rectsOverlap(a, b, padding = 0) {
    return a.left < b.right - padding && a.right > b.left + padding
        && a.top < b.bottom - padding && a.bottom > b.top + padding;
}

/** Render-time geometry report used by screenshot QA and regression checks. */
window.getTableProjectionDiagnostics = function getTableProjectionDiagnostics() {
    const table3d = document.querySelector(".table-3d");
    const seats = [...document.querySelectorAll(".seat")];
    const piles = document.querySelector(".table-center-piles")?.getBoundingClientRect();
    const overlaps = [];
    seats.forEach((seat, i) => {
        const a = seat.getBoundingClientRect();
        if (piles && rectsOverlap(a, piles, 4)) overlaps.push(`${seat.dataset.sid}:piles`);
        seats.slice(i + 1).forEach((other) => {
            if (rectsOverlap(a, other.getBoundingClientRect(), 4)) {
                overlaps.push(`${seat.dataset.sid}:${other.dataset.sid}`);
            }
        });
    });
    const facingDots = seats.map((seat) => {
        const x = parseFloat(seat.style.getPropertyValue("--seat-world-x"));
        const y = parseFloat(seat.style.getPropertyValue("--seat-world-y"));
        const yaw = parseFloat(seat.style.getPropertyValue("--seat-yaw")) * Math.PI / 180;
        const top = { x: Math.sin(yaw), y: -Math.cos(yaw) };
        const toCenterLength = Math.hypot(x, y) || 1;
        return Number((top.x * (-x / toCenterLength) + top.y * (-y / toCenterLength)).toFixed(5));
    });
    const cardWidths = seats.map((seat) => ({
        x: parseFloat(seat.style.getPropertyValue("--seat-world-x")),
        y: parseFloat(seat.style.getPropertyValue("--seat-world-y")),
        width: seat.querySelector(".board-card")?.getBoundingClientRect().width || 0,
    })).sort((a, b) => a.y - b.y);
    const axialCards = cardWidths.filter((card) => Math.abs(card.x) < 0.05);
    const measuredNearFarRatio = axialCards.length === 2 && axialCards[0].width
        ? axialCards[1].width / axialCards[0].width
        : null;
    const expectedAxialSeatRatio = axialCards.length === 2
        ? (TABLE.cameraLength ** 2 - TABLE.cameraDistance * axialCards[0].y)
            / (TABLE.cameraLength ** 2 - TABLE.cameraDistance * axialCards[1].y)
        : null;
    return {
        world: { width: TABLE.width, depth: TABLE.depth, card: [TABLE.cardWidth, TABLE.cardHeight] },
        camera: {
            distance: TABLE.cameraDistance,
            height: TABLE.cameraHeight,
            pitchDegrees: Number((TABLE.pitch * 180 / Math.PI).toFixed(4)),
            focalPixels: parseFloat(getComputedStyle(table3d).getPropertyValue("--camera-focal")),
        },
        scalePixelsPerUnit: Number(table3d?.dataset.worldScale || 0),
        expectedNearFarRatio: Number(table3d?.dataset.projectedNearFarRatio || 0),
        expectedAxialSeatRatio: expectedAxialSeatRatio && Number(expectedAxialSeatRatio.toFixed(4)),
        measuredNearFarRatio: measuredNearFarRatio && Number(measuredNearFarRatio.toFixed(4)),
        seatDepthScales: cardWidths.map((card) => Number((
            TABLE.cameraLength ** 2
            / (TABLE.cameraLength ** 2 - TABLE.cameraDistance * card.y)
        ).toFixed(4))),
        facingDots,
        overlaps,
        centerClearance: {
            x: TABLE.centerClearanceX,
            y: TABLE.centerClearanceY,
            pileY: TABLE.pileY,
        },
        boardLayouts: seats.map((seat) => ({
            sid: seat.dataset.sid,
            cards: state.players[seat.dataset.sid]?.board?.length || 0,
            scale: Number(seat.style.getPropertyValue("--seat-scale")),
            ring: Number(seat.style.getPropertyValue("--seat-ring")),
        })),
        rings: seats.map((seat) => Number(seat.style.getPropertyValue("--seat-ring"))),
    };
};

function renderSeat(sid, position, options = {}) {
    const player = state.players[sid];
    const isMe = sid === mySid;
    const classes = ["seat"];
    if (isMe) classes.push("me");
    if (player.protected) classes.push("protected");
    if (player.eliminated) classes.push("spectator-seat");
    if (sid === state.current_turn_sid) classes.push("current-turn");
    if (sid === keyboardNav.ownerSid) classes.push("keyboard-target");

    const hideSlot = options.hideAnimTargets && options.action
        && options.action.type === "swap"
        && options.action.sid === sid
        ? options.action.index
        : -1;
    const hiddenAnimationSlots = [];
    if (options.hideAnimTargets && options.action?.type === "switch") {
        hiddenAnimationSlots.push(
            ...[options.action.a, options.action.b]
                .filter((item) => item?.owner_sid === sid)
                .map((item) => item.index),
        );
    }
    if (options.hideAnimTargets && options.action?.type === "ability_put_back") {
        hiddenAnimationSlots.push(
            ...(options.action.cards || [])
                .filter((item) => item?.owner_sid === sid)
                .map((item) => item.index),
        );
    }
    if (
        options.hideAnimTargets
        && options.action?.type === "burn_give"
        && options.action.target_sid === sid
    ) {
        hiddenAnimationSlots.push(options.action.target_index);
    }
    if (options.hideAnimTargets && options.action?.type === "burn_fail") {
        if (options.action.owner_sid === sid) {
            hiddenAnimationSlots.push(options.action.index);
        }
        if (options.action.sid === sid && options.action.penalty) {
            hiddenAnimationSlots.push(options.action.penalty.index);
        }
    }

    const cols = gridColsForBoard(player.board.length);
    const cards = player.board
        .map((slot, index) => renderBoardCard(sid, index, slot, {
            hidden: index === hideSlot || hiddenAnimationSlots.includes(index),
        }))
        .join("");
    const hideBotHeld = options.hideAnimTargets && options.action
        && (options.action.type === "draw" || options.action.type === "take")
        && options.action.sid === sid;
    const botHeld = player.is_bot
        ? renderBotHeldSlot(sid, { hidden: hideBotHeld })
        : "";

    const style = [
        `--seat-width:var(--grid-width-${cols})`,
        `--seat-left:${position.left.toFixed(2)}%`,
        `--seat-top:${position.top.toFixed(2)}%`,
        `--seat-yaw:${position.yaw.toFixed(1)}deg`,
        `--seat-scale:${position.scale.toFixed(3)}`,
        `--seat-ring:${position.ring.toFixed(4)}`,
        `--seat-world-x:${position.worldX.toFixed(4)}`,
        `--seat-world-y:${position.worldY.toFixed(4)}`,
        `--shadow-x:${(Math.sin((position.yaw * Math.PI) / 180) * 3).toFixed(1)}px`,
        `--shadow-y:${(Math.cos((position.yaw * Math.PI) / 180) * 3).toFixed(1)}px`,
        `z-index:${Math.round(position.top * 10)}`,
    ].join(";");

    return `
        <section class="${classes.join(" ")}" style="${style}" data-sid="${sid}">
            <div class="seat-scale">
                <div class="seat-board">
                    <div class="card-grid cols-${cols}" data-grid="${sid}" style="grid-template-columns:repeat(${cols}, 1fr)">${cards}</div>
                    ${botHeld}
                </div>
            </div>
        </section>
    `;
}

function renderBotHeldSlot(sid, options = {}) {
    const holding = playerIsHolding(sid);
    const peek = state.held_peek?.sid === sid ? state.held_peek : null;
    if (!holding && !peek) return "";
    const hiddenClass = options.hidden ? " anim-hidden" : "";
    const placement = heldPlacementFor(sid);
    return `
        <div
            class="bot-held-slot held-${placement}${hiddenClass}"
            data-held="${sid}"
            style="--held-card-tilt:${TABLE.heldCardTilt}deg"
        >
            <div class="card-back held-card" data-held-card="${sid}"></div>
        </div>
    `;
}

function renderHeldSlot(sid, options = {}) {
    const isMe = sid === mySid;
    const isBot = Boolean(state.players[sid]?.is_bot);
    const holding = playerIsHolding(sid);
    const peek = state.held_peek?.sid === sid ? state.held_peek : null;
    const placement = heldPlacementFor(sid);

    // Local player always shows a distinct hand tray to the left of the grid.
    if (!holding && !peek) {
        if (isMe) {
            return `<div class="held-slot held-${placement} hand-tray empty-tray" data-held="${sid}"><span class="hand-label">hand</span></div>`;
        }
        return `<div class="held-slot empty" data-held="${sid}" aria-hidden="true"></div>`;
    }

    let inner = "";
    let actions = "";

    if (peek && isMe && peek.card) {
        inner = `<div class="playing-card held-card hand-lift ${colorClass(peek.card)}" data-held-card="${sid}">${cardFaceHtml(peek.card)}</div>`;
        const burnBtn = peek.burnable
            ? `<button type="button" class="btn btn-purple tiny-btn" onclick="burnFromPeek()">burn</button>`
            : "";
        actions = `<div class="held-actions">${burnBtn}<button type="button" class="btn btn-gray tiny-btn" onclick="putBackPeek()">put back</button></div>`;
    } else if (holding && isMe && state.pending_draw.card) {
        const card = state.pending_draw.card;
        inner = `<div class="playing-card held-card hand-lift ${colorClass(card)}" data-held-card="${sid}">${cardFaceHtml(card)}</div>`;
        if (card.ability && state.pending_draw.source === "draw") {
            actions = `<div class="held-actions"><button type="button" class="btn btn-green tiny-btn" onclick="playDrawn()">play</button></div>`;
        }
    } else if (holding || peek) {
        inner = `<div class="card-back held-card hand-lift" data-held-card="${sid}"></div>`;
    }

    const hiddenClass = options.hidden ? " anim-hidden" : "";
    const trayClass = isMe ? " hand-tray" : "";
    return `
        <div class="held-slot held-${placement} filled${trayClass}${isBot ? " bot-hand" : ""}${hiddenClass}" data-held="${sid}">
            ${isMe ? `<span class="hand-label">hand</span>` : ""}
            ${inner}
            ${actions}
        </div>
    `;
}

function isBurntSlot(ownerSid, index) {
    return (state.burnt_slots || []).some((s) => s.owner_sid === ownerSid && s.index === index);
}

function isBurnBlocked(ownerSid, index) {
    return (state.burn_blockers || []).some((s) => s.owner_sid === ownerSid && s.index === index);
}

function markLocalBurnAttempt(ownerSid, index, windowMs) {
    if (!ownerSid || !Number.isFinite(index)) return;
    localBurnAttempt = { owner_sid: ownerSid, index };
    document.querySelector(
        `.board-card[data-owner="${CSS.escape(ownerSid)}"][data-index="${index}"]`,
    )?.classList.add("burn-attempt-pending");
    if (localBurnAttemptTimer) window.clearTimeout(localBurnAttemptTimer);
    localBurnAttemptTimer = window.setTimeout(
        clearLocalBurnAttempt,
        Math.max(1200, (Number.isFinite(windowMs) ? windowMs : 850) + 1200),
    );
}

function clearLocalBurnAttempt() {
    localBurnAttempt = null;
    if (localBurnAttemptTimer) window.clearTimeout(localBurnAttemptTimer);
    localBurnAttemptTimer = null;
    document.querySelectorAll(".board-card.burn-attempt-pending").forEach((card) => {
        card.classList.remove("burn-attempt-pending");
    });
}

function isKingTargeted(ownerSid, index) {
    const ability = state.pending_ability;
    if (!ability || ability.type !== "switch_peek") return false;
    return (ability.targets || []).some((item) => (
        item.owner_sid === ownerSid && item.index === index
    ));
}

function renderBoardCard(ownerSid, index, slot, options = {}) {
    if (slot.empty) {
        const holdingPeekHere = state.held_peek
            && state.held_peek.owner_sid === ownerSid
            && state.held_peek.index === index;
        if (holdingPeekHere) {
            return `<div class="board-card empty peeking" data-owner="${ownerSid}" data-index="${index}"></div>`;
        }
        return `<div class="board-card empty" data-owner="${ownerSid}" data-index="${index}"></div>`;
    }

    const selected = isSelectedByAbility(ownerSid, index);
    const kingTargeted = isKingTargeted(ownerSid, index);
    const burnt = isBurntSlot(ownerSid, index);
    const highlight = shouldHighlightSlot(ownerSid, index);
    const opening = canOpeningPeek(ownerSid, index, slot);
    const looked = isRecentlyMarked("looked", ownerSid, index)
        || isKingInspectionCard(ownerSid, index);
    const switched = isRecentlyMarked("switched", ownerSid, index);
    const classes = ["board-card", slot.faceUp ? "face-up" : "face-down"];
    if (slot.faceUp && slot.card) classes.push(colorClass(slot.card));
    if (selected) classes.push("selected");
    if (kingTargeted) classes.push("king-targeted");
    if (
        localBurnAttempt
        && localBurnAttempt.owner_sid === ownerSid
        && localBurnAttempt.index === index
    ) classes.push("burn-attempt-pending");
    if (
        selected
        && !state.pending_burn
        && state.pending_ability?.sid === mySid
        && (state.pending_ability.type === "switch_unseen" || state.pending_ability.type === "switch_peek")
    ) {
        classes.push("ability-picked-up");
    }
    if (burnt) classes.push("burnt");
    if (highlight) classes.push("swap-mark");
    if (looked) classes.push("looked-mark");
    if (switched) classes.push("switched-mark");
    if (opening) classes.push("opening-peek");
    if (ownerSid === keyboardNav.ownerSid && index === keyboardNav.index) classes.push("keyboard-focus");
    if (options.hidden) classes.push("anim-hidden");
    if (isClickable(ownerSid, index, slot) || opening) classes.push("selectable");

    const html = slot.faceUp && slot.card
        ? cardFaceHtml(slot.card)
        : "";

    return `<button type="button" class="${classes.join(" ")}" data-owner="${ownerSid}" data-index="${index}">${html}</button>`;
}

function canOpeningPeek(ownerSid, index, slot) {
    if (slot.empty || slot.faceUp) return false;
    const me = state.players[mySid];
    if (!me?.opening_peekable) return false;
    return Boolean(slot.openingPeekable);
}

function shouldHighlightSlot(ownerSid, index) {
    if (swapMark && swapMark.sid === ownerSid && swapMark.index === index) return true;
    const action = state.last_action;
    if (!action) return false;
    if ((action.type === "burn" || action.type === "burn_fail") && action.owner_sid === ownerSid && action.index === index) {
        return true;
    }
    return false;
}

function isClickable(ownerSid, index, slot) {
    if (slot.empty || animating) return false;
    if (state.players[mySid]?.called || state.players[mySid]?.eliminated) return false;

    if (canOpeningPeek(ownerSid, index, slot)) return true;

    if (state.pending_burn && state.pending_burn.sid === mySid) {
        return ownerSid === mySid;
    }

    // Ability selection takes priority — do not treat as burn targets while selecting.
    if (state.pending_ability?.sid === mySid && state.phase === "ability") {
        const stage = state.pending_ability.stage;
        if (stage === "selecting" || stage === "waiting") {
            const type = state.pending_ability.type;
            if (type === "peek_own") return ownerSid === mySid;
            if (type === "peek_other") return ownerSid !== mySid;
            if (type === "switch_unseen" || type === "switch_peek") {
                if (isSelectedByAbility(ownerSid, index)) return false;
                return !(ownerSid !== mySid && state.players[ownerSid].protected);
            }
        }
        return false;
    }

    if (holdingMyDraw()) {
        return ownerSid === mySid;
    }

    if (
        state.status === "playing"
        && state.discard_top
        && state.discard_burn_available
        && !state.pending_burn
        && !holdingMyDraw()
    ) {
        if (isBurnBlocked(ownerSid, index)) return false;
        return true;
    }

    return false;
}

function isSelectedByAbility(ownerSid, index) {
    const selected = state.pending_ability?.sid === mySid ? state.pending_ability.selected || [] : [];
    return selected.some((item) => item.owner_sid === ownerSid && item.index === index);
}

function cardClicked(ownerSid, index) {
    if (!state || animating) return;
    if (state.players[mySid]?.eliminated) {
        showToast("You are spectating this game.");
        return;
    }
    if (state.players[mySid]?.called) {
        showToast("You already called and cannot take any more actions this round.");
        return;
    }

    const slot = state.players[ownerSid]?.board?.[index];
    if (slot && canOpeningPeek(ownerSid, index, slot)) {
        socket.emit("peek_opening", { room: ROOM_ID, owner_sid: ownerSid, index });
        return;
    }

    if (state.pending_burn && state.pending_burn.sid === mySid) {
        if (ownerSid !== mySid) {
            showToast("Pick one of your cards to give.");
            return;
        }
        socket.emit("finish_burn_give", { room: ROOM_ID, index });
        return;
    }

    // Ability before hold-swap / burn so peek/switch clicks always land.
    if (state.pending_ability?.sid === mySid && state.phase === "ability") {
        if (isSelectedByAbility(ownerSid, index)) return;
        const stage = state.pending_ability.stage;
        if (stage === "selecting" || stage === "waiting") {
            socket.emit("ability_select_card", { room: ROOM_ID, owner_sid: ownerSid, index });
            return;
        }
    }

    if (holdingMyDraw()) {
        if (ownerSid !== mySid) {
            showToast("Switch with one of your own cards.");
            return;
        }
        socket.emit("swap_drawn", { room: ROOM_ID, index });
        return;
    }

    if (
        state.status === "playing"
        && state.discard_top
        && !state.pending_burn
        && !holdingMyDraw()
    ) {
        if (!state.discard_burn_available) {
            showToast("That discard has already had a card burned on it.");
            return;
        }
        socket.emit("burn_card", {
            room: ROOM_ID,
            owner_sid: ownerSid,
            index,
            discard_id: state.discard_top?.id || null,
        });
        return;
    }

    if (state.status === "playing" && !state.discard_top) {
        showToast("Nothing to burn against — discard is empty.");
    }
}

function abilityCandidateKey(item) {
    return `${item?.owner_sid || ""}:${item?.index ?? ""}`;
}

function renderSwitchAbilityTray(ability) {
    const isBlackKing = ability.type === "switch_peek";
    const inspected = isBlackKing
        ? ability.inspected || []
        : ability.selected || [];
    const liveCards = new Map(
        (ability.peek_pair || []).map((item) => [abilityCandidateKey(item), item]),
    );
    const burnedCards = new Map(
        (ability.burned_cards || []).map((item) => [abilityCandidateKey(item), item]),
    );
    const movedCards = new Map(
        (ability.moved_cards || []).map((item) => [abilityCandidateKey(item), item]),
    );

    const cards = inspected.map((candidate) => {
        const key = abilityCandidateKey(candidate);
        const live = liveCards.get(key);
        const burned = burnedCards.get(key);
        const moved = movedCards.get(key);
        const ownerName = state.players[candidate.owner_sid]?.username || "";
        if (burned) {
            return `
                <div class="peek-pair-item ability-burned-item">
                    <div class="ability-burned-card">Burned</div>
                    <p>${escapeHtml(ownerName)} #${candidate.index + 1}</p>
                </div>
            `;
        }
        if (moved) {
            return `
                <div class="peek-pair-item ability-burned-item">
                    <div class="ability-burned-card ability-given-card">Given</div>
                    <p>${escapeHtml(ownerName)} #${candidate.index + 1}</p>
                </div>
            `;
        }
        if (isBlackKing && live) {
            return `
                <div class="peek-pair-item">
                    <div
                        class="playing-card mini-card ability-tray-card ${colorClass(live.card)}"
                        data-ability-owner="${escapeHtml(live.owner_sid)}"
                        data-ability-index="${live.index}"
                    >${cardFaceHtml(live.card)}</div>
                    <p>${escapeHtml(ownerName)} #${live.index + 1}</p>
                    ${live.burnable
                        ? `<button type="button" class="btn btn-purple tiny-btn" onclick="burnCard('${live.owner_sid}', ${live.index})">Burn</button>`
                        : ""}
                </div>
            `;
        }
        return `
            <div class="peek-pair-item">
                <div
                    class="card-back mini-card ability-tray-card"
                    data-ability-owner="${escapeHtml(candidate.owner_sid)}"
                    data-ability-index="${candidate.index}"
                ></div>
                <p>${escapeHtml(ownerName)} #${candidate.index + 1}</p>
            </div>
        `;
    }).join("");

    const count = isBlackKing
        ? ability.inspection_count || 0
        : (ability.selected || []).length;
    let prompt = isBlackKing
        ? `Inspect cards one at a time (${count}/2).`
        : `Choose cards one at a time (${count}/2).`;
    if (ability.stage === "switching") prompt = "Switching the selected cards…";
    if (ability.stage === "deciding" && ability.can_switch) {
        prompt = "Swap these cards, or put them back.";
    } else if (ability.stage === "deciding") {
        prompt = (ability.selected || []).length
            ? "A selected card was burned. Put back the remaining card."
            : "The selected cards are resolved. Finish the ability.";
    }

    let actions = "";
    if (isBlackKing && ability.stage === "deciding") {
        const putBackLabel = (ability.selected || []).length ? "Put Back" : "Finish";
        actions = `
            ${ability.can_switch
                ? `<button class="btn btn-green tiny-btn" onclick="blackKingDecision(true)">Swap</button>`
                : ""}
            <button class="btn btn-gray tiny-btn" onclick="blackKingDecision(false)">${putBackLabel}</button>
        `;
    } else if (ability.stage !== "switching") {
        actions = `<button class="btn btn-gray tiny-btn" onclick="skipAbility()">Put Back</button>`;
    }

    return `
        <div class="overlay-card compact ability-tray" data-ability-tray>
            <h2>${isBlackKing ? "Black King" : "Jack / Queen"}</h2>
            <p>${prompt}</p>
            <div class="peek-pair-row ability-picked-row">${cards}</div>
            <div class="panel-actions">${actions}</div>
        </div>
    `;
}

function layoutAbilityTray() {
    if (!els.abilityOverlay) return;
    const tray = els.abilityOverlay.querySelector("[data-ability-tray]");
    if (!tray) return;
    const overlayRect = els.abilityOverlay.getBoundingClientRect();
    const hand = document.querySelector(`[data-held="${CSS.escape(mySid)}"]`);
    const seat = document.querySelector(`.seat[data-sid="${CSS.escape(mySid)}"]`);
    const anchor = hand?.getBoundingClientRect() || seat?.getBoundingClientRect();
    if (!anchor || overlayRect.width < 8) return;

    const trayRect = tray.getBoundingClientRect();
    const desiredLeft = anchor.left - overlayRect.left - trayRect.width / 2 - 14;
    const desiredTop = anchor.top - overlayRect.top + anchor.height / 2;
    const halfWidth = trayRect.width / 2;
    const halfHeight = trayRect.height / 2;
    const left = Math.max(
        halfWidth + 8,
        Math.min(overlayRect.width - halfWidth - 8, desiredLeft),
    );
    const top = Math.max(
        halfHeight + 8,
        Math.min(overlayRect.height - halfHeight - 8, desiredTop),
    );
    tray.style.left = `${left.toFixed(2)}px`;
    tray.style.top = `${top.toFixed(2)}px`;
}

function renderAbilityOverlay() {
    if (!els.abilityOverlay) return;
    els.abilityOverlay.classList.remove("pass-through");

    if (state.status === "round_over" || state.status === "game_over") {
        els.abilityOverlay.classList.remove("hidden");
        els.abilityOverlay.innerHTML = renderRoundOverHtml();
        return;
    }

    if (state.pending_burn && state.pending_burn.sid === mySid) {
        const target = state.players[state.pending_burn.target_sid]?.username || "them";
        els.abilityOverlay.classList.remove("hidden");
        els.abilityOverlay.classList.add("pass-through");
        els.abilityOverlay.innerHTML = `
            <div class="overlay-card compact docked">
                <h2>Burn hit</h2>
                <p>Click one of your cards to give to ${escapeHtml(target)}.</p>
            </div>
        `;
        return;
    }

    const ability = state.pending_ability;
    if (
        ability?.sid === mySid
        && (ability.type === "switch_unseen" || ability.type === "switch_peek")
        && (
            ability.stage === "selecting"
            || ability.stage === "switching"
            || ability.stage === "deciding"
        )
    ) {
        els.abilityOverlay.classList.remove("hidden");
        els.abilityOverlay.classList.add("pass-through");
        els.abilityOverlay.innerHTML = renderSwitchAbilityTray(ability);
        requestAnimationFrame(layoutAbilityTray);
        return;
    }

    if (ability?.sid === mySid && ability.stage === "selecting") {
        let prompt = "Select a card.";
        if (ability.type === "peek_own") prompt = "Select one of your cards to peek.";
        if (ability.type === "peek_other") prompt = "Select an opponent's card to peek.";
        // Docked hint — must NOT cover the table (pointer-events pass through).
        els.abilityOverlay.classList.remove("hidden");
        els.abilityOverlay.classList.add("pass-through");
        els.abilityOverlay.innerHTML = `
            <div class="overlay-card compact docked">
                <p>${prompt}</p>
                <button class="btn btn-gray tiny-btn" onclick="skipAbility()">Skip</button>
            </div>
        `;
        return;
    }

    // Spectators watching bots / other players use abilities
    const spectatorNote = spectatorAbilityNote();
    if (spectatorNote) {
        els.abilityOverlay.classList.remove("hidden");
        els.abilityOverlay.classList.add("pass-through");
        els.abilityOverlay.innerHTML = `
            <div class="overlay-card compact docked">
                <p>${escapeHtml(spectatorNote)}</p>
            </div>
        `;
        return;
    }

    els.abilityOverlay.classList.add("hidden");
    els.abilityOverlay.classList.remove("pass-through");
    els.abilityOverlay.innerHTML = "";
}

function spectatorAbilityNote() {
    const peek = state.held_peek;
    if (peek && peek.sid !== mySid) {
        const who = state.players[peek.sid]?.username || "Someone";
        if (peek.owner_sid === peek.sid) {
            return `${who} is looking at one of their cards…`;
        }
        const owner = state.players[peek.owner_sid]?.username || "a player";
        return `${who} is looking at ${owner}'s card…`;
    }
    const ability = state.pending_ability;
    if (!ability || ability.sid === mySid) return "";
    const who = state.players[ability.sid]?.username || "Someone";
    const label = ability.label || "a special";
    const kingTargets = ability.targets?.length || 0;
    if (
        ability.type === "switch_peek" && kingTargets
    ) {
        return `${who}'s King targeted ${kingTargets} of 2 cards — red marks the selections…`;
    }
    if (ability.stage === "holding") {
        return `${who} is resolving ${label}…`;
    }
    if (ability.stage === "selecting" || ability.stage === "waiting") {
        if (ability.type === "peek_own") return `${who} is choosing one of their cards to peek…`;
        if (ability.type === "peek_other") return `${who} is choosing a card to peek…`;
        if (ability.type === "switch_unseen") return `${who} is choosing two cards to switch…`;
        if (ability.type === "switch_peek") return `${who} is choosing two cards to look at…`;
        return `${who} is using ${label}…`;
    }
    if (ability.stage === "deciding") {
        return `${who} is deciding whether to swap…`;
    }
    return "";
}

function renderHint() {
    if (!els.actionHint) return;
    let text = "";
    if (state.pending_burn && state.pending_burn.sid !== mySid) {
        text = `${state.players[state.pending_burn.sid]?.username || "Someone"} is finishing a burn…`;
    } else if (spectatorAbilityNote()) {
        text = spectatorAbilityNote();
    } else if (state.pending_ability?.sid === mySid && state.phase === "ability" && state.pending_ability.stage === "selecting") {
        text = "Click a board card for the ability.";
    } else if (state.players[mySid]?.eliminated) {
        text = "You busted and are spectating the remaining players.";
    } else if (state.players[mySid]?.opening_peekable) {
        text = "Click any highlighted opening-peek cards, then draw when ready.";
    } else if (holdingMyDraw()) {
        const card = state.pending_draw.card;
        if (card?.ability && state.pending_draw.source === "draw") {
            text = "Play (discard pile / play button) to use ability, or click your board to swap.";
        } else if (state.pending_draw.source === "draw") {
            text = "Click your board to swap, or play to discard.";
        } else {
            text = "Click your board to swap. Discard pickups cannot be played.";
        }
    } else if (canChooseNow()) {
        text = state.discard_burn_available
            ? "Draw, take discard, or call. Click any board card to attempt a burn."
            : "Draw, take discard, or call. This discard cannot be burned again.";
    } else if (swapMark && swapMark.sid !== mySid) {
        const name = state.players[swapMark.sid]?.username || "Player";
        text = `${name} swapped slot ${swapMark.index + 1}`;
    }
    els.actionHint.textContent = text;
}

function renderRoundOverHtml() {
    const hostButton = state.host_sid === mySid && state.status === "round_over"
        ? `<button class="btn btn-green" onclick="nextRound()">Next round</button>`
        : "";
    const rows = state.player_order.map((sid) => {
        const player = state.players[sid];
        const hasRoundScore = state.round_results?.raw_scores?.[sid] != null;
        const raw = state.round_results?.raw_scores?.[sid] ?? 0;
        const round = state.round_results?.round_scores?.[sid] ?? 0;
        const result = hasRoundScore ? `hand ${raw} / +${round}` : "spectating";
        const busted = (state.round_results?.eliminated || []).includes(sid) ? " · BUST" : "";
        return `<div class="result-row ${player.eliminated ? "eliminated" : ""}"><span>${escapeHtml(player.username)}</span><strong>${result}${busted}</strong></div>`;
    }).join("");
    const winnerText = state.status === "game_over" && state.winner_summary
        ? `<p>${state.winner_summary.winners.map((sid) => escapeHtml(state.players[sid].username)).join(", ")} ${state.winner_summary.winners.length === 1 ? "wins" : "win"}.</p>`
        : `<p>${escapeHtml(state.players[state.round_results?.next_start_sid]?.username || "Next")} starts next.</p>`;
    return `
        <div class="overlay-card">
            <h2>${state.status === "game_over" ? "Game over" : "Round over"}</h2>
            ${winnerText}
            ${rows}
            <div class="panel-actions">${hostButton}</div>
        </div>
    `;
}

/* ========== Flying card animations ========== */

const CARD_ASPECT = 0.72; // width / height

function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** Force card proportions — 3D AABB rects often look square or inflated. */
function cardSized(w, h) {
    const aw = Math.max(8, w || 56);
    const ah = Math.max(8, h || 78);
    // Cap runaway AABBs from 3D trays / yaw so flights don't surge in size.
    const cappedW = Math.min(aw, 120);
    const cappedH = Math.min(ah, 160);
    const mid = Math.sqrt(cappedW * cappedH);
    const nh = Math.min(140, Math.max(56, mid / Math.sqrt(CARD_ASPECT)));
    return { w: nh * CARD_ASPECT, h: nh };
}

function readYaw(el) {
    if (!el) return 0;
    const raw = getComputedStyle(el).getPropertyValue("--seat-yaw").trim();
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : 0;
}

function isRecentlyMarked(type, ownerSid, index) {
    return recentCardMarks[type].some((item) => item.owner_sid === ownerSid && item.index === index);
}

function isKingInspectionCard(ownerSid, index) {
    const ability = state.pending_ability;
    if (!ability || ability.type !== "switch_peek" || ability.stage !== "deciding") return false;
    return (ability.selected || []).some((item) => item.owner_sid === ownerSid && item.index === index);
}

function seatWorldFrame(sid) {
    const seat = document.querySelector(`.seat[data-sid="${CSS.escape(sid)}"]`);
    if (!seat) return null;
    const yaw = readYaw(seat);
    const radians = yaw * Math.PI / 180;
    return {
        seat,
        x: parseFloat(seat.style.getPropertyValue("--seat-world-x")),
        y: parseFloat(seat.style.getPropertyValue("--seat-world-y")),
        yaw,
        scale: parseFloat(seat.style.getPropertyValue("--seat-scale")) || 1,
        tangent: { x: Math.cos(radians), y: Math.sin(radians) },
        outward: { x: -Math.sin(radians), y: Math.cos(radians) },
    };
}

function worldFromSeatLocal(frame, localX, localY, tilt = 0) {
    if (!frame) return null;
    return {
        x: frame.x + frame.tangent.x * localX + frame.outward.x * localY,
        y: frame.y + frame.tangent.y * localX + frame.outward.y * localY,
        yaw: frame.yaw,
        tilt,
    };
}

function uprightHandYaw(yaw) {
    let normalized = ((yaw + 180) % 360 + 360) % 360 - 180;
    if (normalized > 90) normalized -= 180;
    if (normalized < -90) normalized += 180;
    return normalized;
}

function boardWorldAnchor(sid, index) {
    const frame = seatWorldFrame(sid);
    if (!frame) return null;
    const boardLength = state?.players?.[sid]?.board?.length || 4;
    const cols = gridColsForBoard(boardLength);
    const rows = Math.max(1, Math.ceil(boardLength / cols));
    const col = index % cols;
    const row = Math.floor(index / cols);
    const localX = (col - (cols - 1) / 2) * (TABLE.cardWidth + TABLE.cardGap) * frame.scale;
    const localY = (row - (rows - 1) / 2) * (TABLE.cardHeight + TABLE.cardGap) * frame.scale;
    return worldFromSeatLocal(frame, localX, localY);
}

function heldWorldAnchor(sid) {
    const frame = seatWorldFrame(sid);
    if (!frame) return null;
    const boardLength = state?.players?.[sid]?.board?.length || 4;
    const cols = gridColsForBoard(boardLength);
    const rows = Math.max(1, Math.ceil(boardLength / cols));
    const boardWidth = cols * TABLE.cardWidth + (cols - 1) * TABLE.cardGap;
    const boardHeight = rows * TABLE.cardHeight + (rows - 1) * TABLE.cardGap;
    const placement = heldPlacementFor(sid);
    const localX = placement === "side"
        ? -(boardWidth / 2 + 0.14 + TABLE.cardWidth / 2) * frame.scale
        : 0;
    const localY = placement === "below"
        ? (boardHeight / 2 + TABLE.heldCardGap + TABLE.cardHeight / 2) * frame.scale
        : 0;
    const isBot = state.players[sid]?.is_bot;
    const anchor = worldFromSeatLocal(
        frame,
        localX,
        localY,
        isBot ? TABLE.heldCardTilt : -TABLE.pitch * 180 / Math.PI,
    );
    if (!isBot) anchor.yaw = uprightHandYaw(frame.yaw);
    return anchor;
}

function burnShowdownHtml(showdown) {
    const attempts = (showdown.attempts || [])
        .slice()
        .sort((a, b) => a.time_ms - b.time_ms);
    const rows = attempts.map((attempt, index) => {
        const name = state.players[attempt.sid]?.username || "Player";
        let result = "Attempt";
        if (attempt.sid === showdown.winner_sid) result = "First valid burn";
        else if (attempt.result === "late") result = "Late burn · penalty card";
        else if (attempt.result === "miss") result = "Wrong burn · penalty card";
        else if (attempt.result === "cancelled") result = "Attempt cancelled";
        const delta = Number(attempt.delta_ms) || 0;
        const deltaLabel = `${delta > 0 ? "+" : ""}${delta}ms`;
        return `
            <div class="burn-showdown-row ${attempt.sid === showdown.winner_sid ? "winner" : "loser"}" style="--race-order:${index}">
                <span class="burn-showdown-place">${index + 1}</span>
                <span class="burn-showdown-player"><strong>${escapeHtml(name)}</strong><small>${result}</small></span>
                <b>${escapeHtml(deltaLabel)}</b>
            </div>
        `;
    }).join("");
    const card = showdown.discard_card?.label || "discard";
    const winnerAttempt = attempts.find((attempt) => attempt.sid === showdown.winner_sid);
    const winnerName = showdown.winner_sid
        ? state.players[showdown.winner_sid]?.username || "Player"
        : "";
    const target = showdown.winner_target || {};
    const ownerName = state.players[target.owner_sid]?.username || "a player";
    const targetLabel = target.card?.label || "card";
    const targetText = target.owner_sid === showdown.winner_sid
        ? `their own ${targetLabel}`
        : `${ownerName}'s ${targetLabel}`;
    const winningSeconds = (Math.max(0, winnerAttempt?.time_ms || 0) / 1000).toFixed(2);
    const isRace = attempts.length > 1;
    const heading = showdown.winner_sid
        ? `<h2><span>${escapeHtml(winnerName)}</span> burned ${escapeHtml(targetText)}</h2>`
        : "<h2>No burn landed</h2>";
    const reaction = showdown.winner_sid
        ? `<div class="burn-winning-time">${winningSeconds}s<small>SERVER REACTION</small></div>`
        : "";
    return `
        <div class="burn-showdown-card">
            <div class="burn-impact" aria-hidden="true">${isRace ? "SHOWDOWN!" : "BURN!"}</div>
            <div class="burn-showdown-kicker">${isRace ? "SERVER BURN SHOWDOWN" : "BURN CONFIRMED"} · ${escapeHtml(card)}</div>
            ${heading}
            ${reaction}
            <div class="burn-showdown-list">${rows}</div>
        </div>
    `;
}

function renderBurnShowdown() {
    if (!els.burnShowdown || !state?.burn_showdown) return;
    const showdown = state.burn_showdown;
    if (showdown.id === lastBurnShowdownId) {
        if (!els.burnShowdown.classList.contains("hidden")) {
            els.burnShowdown.innerHTML = burnShowdownHtml(showdown);
        }
        return;
    }
    lastBurnShowdownId = showdown.id;
    els.burnShowdown.innerHTML = burnShowdownHtml(showdown);
    els.burnShowdown.classList.remove("hidden");
    if (burnShowdownTimer) window.clearTimeout(burnShowdownTimer);
    burnShowdownTimer = window.setTimeout(() => {
        els.burnShowdown.classList.add("hidden");
    }, (showdown.attempts || []).length > 1 ? 6500 : 4500);
}

function captureAnchors() {
    const rect = (el, yawEl = null, world = null) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return null;
        const sized = cardSized(r.width, r.height);
        const yaw = readYaw(yawEl || el.closest?.(".seat"));
        return {
            x: r.left + (r.width - sized.w) / 2,
            y: r.top + (r.height - sized.h) / 2,
            w: sized.w,
            h: sized.h,
            yaw,
            world,
        };
    };
    const out = {
        draw: rect(els.drawBtn, null, { x: -0.46, y: TABLE.pileY, yaw: 0, tilt: 0 }),
        discard: rect(els.discardBtn, null, { x: 0.46, y: TABLE.pileY, yaw: 0, tilt: 0 }),
        held: {},
        boards: {},
        seats: {},
    };
    if (!state) return out;
    for (const sid of state.player_order) {
        const seat = document.querySelector(`.seat[data-sid="${CSS.escape(sid)}"]`);
        out.held[sid] = rect(
            document.querySelector(`[data-held-card="${CSS.escape(sid)}"]`)
            || document.querySelector(`[data-held="${CSS.escape(sid)}"]`),
            seat,
            state.players[sid]?.is_bot ? heldWorldAnchor(sid) : null,
        );
        out.boards[sid] = {};
        const len = state.players[sid]?.board?.length || 4;
        for (let i = 0; i < len; i++) {
            const trayCard = document.querySelector(
                `[data-ability-owner="${CSS.escape(sid)}"][data-ability-index="${i}"]`,
            );
            out.boards[sid][i] = rect(
                trayCard
                || document.querySelector(`.board-card[data-owner="${CSS.escape(sid)}"][data-index="${i}"]`),
                seat,
                trayCard ? null : boardWorldAnchor(sid, i),
            );
        }
        const frame = seatWorldFrame(sid);
        out.seats[sid] = rect(seat, seat, frame && {
            x: frame.x,
            y: frame.y,
            yaw: frame.yaw,
            tilt: 0,
        });
    }
    return out;
}

function captureFullAnchors() {
    return captureAnchors();
}

function finishFlyingCard(el, lingerMs, statusText, resolve) {
    if (statusText) {
        const status = document.createElement("span");
        status.className = "flight-status";
        status.textContent = statusText;
        el.appendChild(status);
    }
    if (lingerMs > 0) {
        setTimeout(() => {
            el.remove();
            resolve();
        }, lingerMs);
        return;
    }
    el.remove();
    resolve();
}

function flyCardOnPlane({
    from,
    to,
    html,
    className = "",
    duration = ANIM_MS,
    lingerMs = 0,
    statusText = "",
}) {
    return new Promise((resolve) => {
        const surface = document.querySelector(".table-surface");
        const table3d = document.querySelector(".table-3d");
        if (!surface || !table3d || !from?.world || !to?.world) {
            resolve();
            return;
        }
        const start = from.world;
        const end = to.world;
        const scale = Number(table3d.dataset.worldScale) || 80;
        const el = document.createElement("div");
        el.className = `fly-card plane-flight ${className}`.trim();
        el.innerHTML = html;
        surface.appendChild(el);

        const setPose = (x, y, yaw, tilt, lift) => {
            el.style.transform = `translate3d(${x * scale}px, ${y * scale}px, ${lift}px) translate(-50%, -50%) rotateZ(${yaw}deg) rotateX(${tilt}deg)`;
        };
        setPose(start.x, start.y, start.yaw || 0, start.tilt || 0, 2);
        const t0 = performance.now();

        function frame(now) {
            const t = Math.min(1, (now - t0) / duration);
            const e = easeInOutCubic(t);
            const x = start.x + (end.x - start.x) * e;
            const y = start.y + (end.y - start.y) * e;
            const yaw = (start.yaw || 0) + shortestDeg((end.yaw || 0) - (start.yaw || 0)) * e;
            const tilt = (start.tilt || 0) + ((end.tilt || 0) - (start.tilt || 0)) * e;
            const lift = 2 + Math.sin(Math.PI * e) * scale * 0.34;
            setPose(x, y, yaw, tilt, lift);
            if (t < 1) {
                requestAnimationFrame(frame);
            } else {
                finishFlyingCard(el, lingerMs, statusText, resolve);
            }
        }
        requestAnimationFrame(frame);
    });
}

function flyCard({
    from,
    to,
    html,
    className = "",
    duration = ANIM_MS,
    lingerMs = 0,
    statusText = "",
}) {
    if (from?.world && to?.world) {
        return flyCardOnPlane({
            from,
            to,
            html,
            className,
            duration,
            lingerMs,
            statusText,
        });
    }
    return new Promise((resolve) => {
        if (!els.flyLayer || !from || !to) {
            resolve();
            return;
        }
        const start = cardSized(from.w, from.h);
        const end = cardSized(to.w, to.h);
        // Prefer the measured on-screen size so we don't jump; still force aspect.
        const w0 = start.w;
        const h0 = start.h;
        const w1 = end.w;
        const h1 = end.h;
        const yaw0 = from.yaw || 0;
        const yaw1 = to.yaw || 0;

        const el = document.createElement("div");
        el.className = `fly-card ${className}`.trim();
        el.innerHTML = html;
        el.style.width = `${w0}px`;
        el.style.height = `${h0}px`;
        el.style.left = `${from.x}px`;
        el.style.top = `${from.y}px`;
        // Screen-parallel flight — rotateZ only. rotateX made cards surge at the camera then vanish.
        el.style.transform = `rotateZ(${yaw0}deg)`;
        els.flyLayer.appendChild(el);

        const t0 = performance.now();
        const x0 = from.x;
        const y0 = from.y;
        const x1 = to.x + ((to.w || w1) - w1) / 2;
        const y1 = to.y + ((to.h || h1) - h1) / 2;
        const midLift = -Math.min(28, Math.hypot(x1 - x0, y1 - y0) * 0.06);

        function frame(now) {
            const t = Math.min(1, (now - t0) / duration);
            const e = easeInOutCubic(t);
            const x = x0 + (x1 - x0) * e;
            const y = y0 + (y1 - y0) * e + midLift * Math.sin(Math.PI * e);
            const w = w0 + (w1 - w0) * e;
            const h = h0 + (h1 - h0) * e;
            const yaw = yaw0 + shortestDeg(yaw1 - yaw0) * e;
            el.style.left = `${x}px`;
            el.style.top = `${y}px`;
            el.style.width = `${w}px`;
            el.style.height = `${h}px`;
            el.style.transform = `rotateZ(${yaw}deg)`;
            if (t < 1) {
                requestAnimationFrame(frame);
            } else {
                finishFlyingCard(el, lingerMs, statusText, resolve);
            }
        }
        requestAnimationFrame(frame);
    });
}

function faceDownHtml() {
    return `<div class="card-back held-card flying-face"></div>`;
}

function faceUpHtml(card) {
    if (!card) return faceDownHtml();
    return `<div class="playing-card held-card flying-face ${colorClass(card)}">${cardFaceHtml(card)}</div>`;
}

function destHeld(sid, afterAnchors) {
    const held = afterAnchors.held[sid];
    if (held?.world) return held;
    // Prefer a real held-card rect; ignore tiny/warped AABBs from hidden trays.
    if (held && held.w >= 40 && held.h >= 50) return held;

    const seat = afterAnchors.seats[sid];
    if (seat) {
        const sized = cardSized(60, 84);
        return {
            x: seat.x - sized.w - 18,
            y: seat.y + Math.max(0, (seat.h - sized.h) / 2),
            w: sized.w,
            h: sized.h,
            yaw: 0,
        };
    }
    return { x: window.innerWidth / 2 - 28, y: window.innerHeight / 2 - 40, w: 56, h: 78, yaw: 0 };
}

function destBoard(sid, index, afterAnchors) {
    const slot = afterAnchors.boards[sid] && afterAnchors.boards[sid][index];
    if (slot?.world) return slot;
    if (slot && slot.w >= 30 && slot.h >= 40) return slot;
    return destHeld(sid, afterAnchors);
}

async function playActionAnimation(action, beforeAnchors) {
    animating = true;
    const safety = setTimeout(() => { animating = false; }, 6000);
    const after = captureFullAnchors();
    const before = beforeAnchors || {};
    const srcDraw = before.draw || after.draw;
    const srcDiscard = before.discard || after.discard;

    try {
        if (action.type === "draw") {
            const to = destHeld(action.sid, after);
            const html = action.sid === mySid && state.pending_draw?.card
                ? faceUpHtml(state.pending_draw.card)
                : faceDownHtml();
            await flyCard({ from: srcDraw, to, html, duration: PICKUP_ANIM_MS });
        } else if (action.type === "take") {
            const to = destHeld(action.sid, after);
            await flyCard({
                from: srcDiscard,
                to,
                html: action.sid === mySid && action.card ? faceUpHtml(action.card) : faceDownHtml(),
                duration: PICKUP_ANIM_MS,
            });
        } else if (action.type === "swap") {
            const heldRect = (before.held && before.held[action.sid]) || after.held[action.sid] || after.seats[action.sid];
            const slotFrom = (before.boards && before.boards[action.sid] && before.boards[action.sid][action.index])
                || after.boards[action.sid]?.[action.index];
            const slotTo = destBoard(action.sid, action.index, after);
            const discardTo = after.discard || srcDiscard;

            await Promise.all([
                flyCard({
                    from: heldRect || srcDraw,
                    to: slotTo,
                    html: faceDownHtml(),
                    duration: SWITCH_ANIM_MS,
                }),
                flyCard({
                    from: slotFrom || slotTo,
                    to: discardTo,
                    html: faceUpHtml(action.outgoing),
                    duration: CENTER_ANIM_MS,
                }),
            ]);
        } else if (action.type === "play") {
            const from = (before.held && before.held[action.sid]) || after.held[action.sid] || after.seats[action.sid] || srcDraw;
            await flyCard({
                from,
                to: after.discard || srcDiscard,
                html: faceUpHtml(action.card),
                duration: CENTER_ANIM_MS,
            });
        } else if (action.type === "burn") {
            const from = (before.boards && before.boards[action.owner_sid] && before.boards[action.owner_sid][action.index])
                || destBoard(action.owner_sid, action.index, after);
            await flyCard({
                from,
                to: after.discard || srcDiscard,
                html: faceUpHtml(action.card),
                duration: CENTER_ANIM_MS,
            });
        } else if (action.type === "burn_fail") {
            const attemptedFrom = before.boards?.[action.owner_sid]?.[action.index]
                || destBoard(action.owner_sid, action.index, after);
            const discardTarget = after.discard || srcDiscard;
            const returnTarget = destBoard(action.owner_sid, action.index, after);
            await flyCard({
                from: attemptedFrom,
                to: discardTarget,
                html: faceUpHtml(action.card),
                className: "failed-burn-flight",
                duration: CENTER_ANIM_MS,
                lingerMs: 850,
                statusText: "Failed Burn",
            });
            await flyCard({
                from: discardTarget,
                to: returnTarget,
                html: faceUpHtml(action.card),
                className: "failed-burn-return",
                duration: SWITCH_ANIM_MS,
            });
            if (action.penalty) {
                const penaltyTarget = destBoard(action.sid, action.penalty.index, after);
                await flyCard({
                    from: srcDraw,
                    to: penaltyTarget,
                    html: faceDownHtml(),
                    className: "penalty-card-flight",
                    duration: PICKUP_ANIM_MS,
                });
            }
        } else if (action.type === "burn_give") {
            const from = before.boards?.[action.sid]?.[action.give_index]
                || destBoard(action.sid, action.give_index, before);
            const to = destBoard(action.target_sid, action.target_index, after);
            await flyCard({
                from,
                to,
                html: faceDownHtml(),
                className: "burn-give-flight",
                duration: SWITCH_ANIM_MS,
            });
        } else if (action.type === "switch") {
            const a = action.a;
            const b = action.b;
            if (a && b) {
                const fromA = before.boards?.[a.owner_sid]?.[a.index]
                    || destBoard(a.owner_sid, a.index, after);
                const fromB = before.boards?.[b.owner_sid]?.[b.index]
                    || destBoard(b.owner_sid, b.index, after);
                const toA = destBoard(b.owner_sid, b.index, after);
                const toB = destBoard(a.owner_sid, a.index, after);
                await Promise.all([
                    flyCard({ from: fromA, to: toA, html: faceDownHtml(), duration: SWITCH_ANIM_MS }),
                    flyCard({ from: fromB, to: toB, html: faceDownHtml(), duration: SWITCH_ANIM_MS }),
                ]);
            }
        } else if (action.type === "ability_put_back") {
            await Promise.all((action.cards || []).map((item) => {
                const from = before.boards?.[item.owner_sid]?.[item.index]
                    || destBoard(item.owner_sid, item.index, after);
                const to = destBoard(item.owner_sid, item.index, after);
                return flyCard({
                    from,
                    to,
                    html: faceDownHtml(),
                    duration: SWITCH_ANIM_MS,
                });
            }));
        } else if (action.type === "peek") {
            const from = destBoard(action.owner_sid, action.index, after);
            const to = destHeld(action.sid, after);
            await flyCard({ from, to, html: faceDownHtml(), duration: PICKUP_ANIM_MS });
        } else if (action.type === "put_back") {
            const from = destHeld(action.sid, after);
            const to = destBoard(action.owner_sid, action.index, after);
            await flyCard({ from, to, html: faceDownHtml(), duration: SWITCH_ANIM_MS });
        } else {
            await wait(ANIM_MS * 0.35);
        }
    } finally {
        clearTimeout(safety);
        animating = false;
        document.querySelectorAll(".anim-hidden").forEach((el) => el.classList.remove("anim-hidden"));
    }
}

function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function putBackPeek() {
    socket.emit("ability_put_back", { room: ROOM_ID });
}

function burnFromPeek() {
    socket.emit("burn_from_peek", { room: ROOM_ID });
}

function burnCard(ownerSid, index) {
    socket.emit("burn_card", {
        room: ROOM_ID,
        owner_sid: ownerSid,
        index,
        discard_id: state?.discard_top?.id || null,
    });
}

function skipAbility() {
    socket.emit("skip_ability", { room: ROOM_ID });
}

function blackKingDecision(shouldSwitch) {
    socket.emit("black_king_decision", { room: ROOM_ID, switch: shouldSwitch });
}

function nextRound() {
    socket.emit("next_round", { room: ROOM_ID });
}

function cardFaceHtml(card) {
    if (!card) return "";
    if (card.rank === "JOKER") {
        return `
            <span class="card-corner tl"><span class="rank">J</span><span class="suit">★</span></span>
            <span class="card-center">Joker</span>
            <span class="card-corner br"><span class="rank">J</span><span class="suit">★</span></span>
        `;
    }
    const symbol = card.suit_symbol || "";
    const rank = card.rank === "10" ? "10" : card.rank;
    return `
        <span class="card-corner tl"><span class="rank">${escapeHtml(rank)}</span><span class="suit">${symbol}</span></span>
        <span class="card-center suit">${symbol}</span>
        <span class="card-corner br"><span class="rank">${escapeHtml(rank)}</span><span class="suit">${symbol}</span></span>
    `;
}

function colorClass(card) {
    if (!card) return "";
    if (card.color === "red") return "red";
    if (card.color === "joker") return "joker";
    return "black";
}

function showToast(message) {
    els.toast.textContent = message;
    els.toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove("show"), 2600);
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
