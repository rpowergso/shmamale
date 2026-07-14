// The whole board is drawn from socket snapshots, so a missing socket client
// (e.g. blocked script) must fail loudly instead of silently killing the page.
const socket = (typeof io !== "undefined")
    ? io()
    : { emit() {}, on() {} };

const ANIM_MS = 1000;

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

const els = {};

if (typeof io === "undefined") {
    setConnBanner("Game client failed to load (socket.io missing). Please refresh the page.", false);
}

document.addEventListener("DOMContentLoaded", () => {
    cacheEls();
    bindStaticEvents();
    bindCardPointerFallback();
    applyTableCameraSettings();
    initializeGameMode();
    window.addEventListener("resize", () => {
        if (!state) return;
        applyTableCameraSettings();
        if (els.seats && state.players) renderSeats();
        else if (els.seats) layoutNameplates(rotatedOrder());
        layoutArLabels();
    });
});

socket.on("connect", () => {
    mySid = socket.id;
    setConnBanner("", true);
    if (state) render();
});

socket.on("connect_error", () => {
    setConnBanner("Can't reach the game server. Make sure it is running, then wait for reconnect…", false);
});

socket.on("disconnect", () => {
    setConnBanner("Disconnected — trying to reconnect…", false);
});

socket.on("game_state", async (nextState) => {
    if (nextState.viewer_sid) {
        mySid = nextState.viewer_sid;
    }
    const action = nextState.last_action;
    const actionKey = action
        ? `${action.type}:${action.epoch}:${action.sid || ""}:${action.index ?? ""}:${action.owner_sid || ""}`
        : "";

    // Capture DOM anchors from the current board before we replace it.
    const anchors = captureAnchors();

    prevState = state;
    state = nextState;
    updateSwapMark(action, nextState);

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
        render({ hideAnimTargets: true, action });
        await playActionAnimation(action, anchors);
        render();
        return;
    }
    render();
});

socket.on("error_message", (data) => {
    showToast(data.msg || "Something went wrong.");
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
    els.hostControls = document.getElementById("host-controls");
    els.settingTarget = document.getElementById("setting-target");
    els.settingDecks = document.getElementById("setting-decks");
    els.settingJokers = document.getElementById("setting-jokers");
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
    els.nameplates = document.getElementById("nameplates");
    els.flyLayer = document.getElementById("fly-layer");
    els.abilityOverlay = document.getElementById("ability-overlay");
    els.actionHint = document.getElementById("action-hint");
    els.toast = document.getElementById("toast");
}

function bindStaticEvents() {
    if (els.readyBtn) {
        els.readyBtn.addEventListener("click", () => {
            ready = !ready;
            socket.emit("toggle_ready", { room: ROOM_ID });
        });
    }
    if (els.startBtn) {
        els.startBtn.addEventListener("click", () => socket.emit("start_game", { room: ROOM_ID }));
    }

    [els.settingTarget, els.settingDecks, els.settingJokers].filter(Boolean).forEach((input) => {
        input.addEventListener("change", () => {
            socket.emit("update_settings", {
                room: ROOM_ID,
                target_score: els.settingTarget.value,
                deck_count: els.settingDecks.value,
                jokers: els.settingJokers.value,
            });
        });
    });

    if (els.drawBtn) els.drawBtn.addEventListener("click", tryDraw);
    if (els.drawPrompt) els.drawPrompt.addEventListener("click", tryDraw);
    if (els.discardBtn) els.discardBtn.addEventListener("click", onDiscardPileClick);
    if (els.takePrompt) els.takePrompt.addEventListener("click", tryTake);
    if (els.playPrompt) els.playPrompt.addEventListener("click", playDrawn);
    if (els.callBtn) els.callBtn.addEventListener("click", () => socket.emit("call_round", { room: ROOM_ID }));
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

function cardAtPoint(clientX, clientY) {
    // Piles sit in the middle — never treat them as board cards.
    if (pileControlAtPoint(clientX, clientY)) return null;

    const stack = document.elementsFromPoint(clientX, clientY);
    const fromStack = stack.find((el) => el.classList?.contains("board-card"));
    if (fromStack) return fromStack;

    let best = null;
    let bestDist = Infinity;
    document.querySelectorAll(".board-card").forEach((card) => {
        const r = card.getBoundingClientRect();
        if (clientX < r.left || clientX > r.right || clientY < r.top || clientY > r.bottom) return;
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

        const card = cardAtPoint(event.clientX, event.clientY);
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
    socket.emit("join", {
        room: ROOM_ID,
        username,
        bot_mode: Boolean(options.botMode),
        bot_count: options.botCount || 0,
        bot_difficulty: options.botDifficulty || "medium",
    });
}

function render(options = {}) {
    if (!state) return;
    if (state.status === "lobby") {
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
    els.hostControls.classList.toggle("hidden", !amHost);
    els.startBtn.classList.toggle("hidden", !amHost);

    els.settingTarget.value = state.settings.target_score;
    els.settingDecks.value = state.settings.deck_count;
    els.settingJokers.value = state.settings.jokers;

    els.playerList.innerHTML = state.player_order.map((sid) => {
        const player = state.players[sid];
        const readyText = player.ready ? "READY" : "WAITING";
        return `
            <div class="lobby-player ${player.is_host ? "host" : ""}">
                <span>${escapeHtml(player.username)} ${player.is_host ? "HOST" : ""} ${player.is_bot ? "BOT" : ""}</span>
                <b style="color:${player.ready ? "#2ecc71" : "#e74c3c"}">${readyText}</b>
            </div>
        `;
    }).join("");

    const me = state.players[mySid];
    if (me) {
        ready = me.ready;
        els.readyBtn.textContent = ready ? "NOT READY" : "READY UP";
        els.readyBtn.className = `btn ${ready ? "btn-red" : "btn-blue"}`;
    }
}

function renderGame(options = {}) {
    const current = state.players[state.current_turn_sid];
    const myTurn = state.current_turn_sid === mySid;
    els.turnText.textContent = myTurn ? "YOUR TURN" : `${current ? current.username.toUpperCase() : "WAITING"}'S TURN`;
    els.turnText.style.color = myTurn ? "#2ecc71" : "#f1c40f";
    els.phaseText.textContent = phaseText();

    renderScores();
    renderPiles();
    renderSeats(options);
    layoutArLabels();
    renderAbilityOverlay();
    renderHint();
    renderCallBtn();
}

function phaseText() {
    if (state.status === "round_over") return "Round over";
    if (state.status === "game_over") return "Game over";
    if (state.first_caller_sid) {
        return `${state.players[state.first_caller_sid]?.username || "Someone"} called — final turns`;
    }
    if (state.held_peek?.sid === mySid) return "Peeking — put back or burn";
    if (state.phase === "drawn") return "Holding a card";
    if (state.phase === "ability") return "Special ability";
    return `Round ${state.round_number}`;
}

function renderScores() {
    if (!els.scoreChips) return;
    els.scoreChips.innerHTML = state.player_order.map((sid) => {
        const player = state.players[sid];
        return `
            <div class="score-chip ${sid === mySid ? "me" : ""}" title="${escapeHtml(player.username)}">
                <span class="score-name">${escapeHtml(player.username)}</span>
                <b>${player.score}</b>
            </div>
        `;
    }).join("");
}

function renderCallBtn() {
    const canChoose = state.status === "playing" && state.phase === "choose" && state.current_turn_sid === mySid;
    els.callBtn.disabled = !canChoose || Boolean(state.pending_burn) || animating;
    els.callBtn.textContent = state.first_caller_sid ? "PROTECT" : "CALL";
}

function canChooseNow() {
    return (
        state.status === "playing"
        && state.phase === "choose"
        && state.current_turn_sid === mySid
        && !state.pending_burn
    );
}

function holdingMyDraw() {
    return state.pending_draw && state.pending_draw.sid === mySid && state.pending_draw.card;
}

function playerIsHolding(sid) {
    return Boolean(state.pending_draw && state.pending_draw.sid === sid);
}

function renderPiles() {
    els.drawCount.textContent = String(state.draw_count);
    els.discardCount.textContent = String(state.discard_count);

    const choose = canChooseNow();
    const holding = holdingMyDraw();
    const canPlay = holding && state.pending_draw.source === "draw";

    els.drawPrompt.classList.toggle("hidden", !choose || state.draw_count === 0);
    els.takePrompt.classList.toggle("hidden", !choose || !state.discard_top);
    els.playPrompt.classList.toggle("hidden", !canPlay);

    els.drawBtn.disabled = !choose || state.draw_count === 0;
    els.discardBtn.disabled = !(choose && state.discard_top) && !canPlay;

    if (state.discard_top) {
        els.discardBtn.className = `playing-card pile-card ${colorClass(state.discard_top)}`;
        els.discardBtn.innerHTML = cardFaceHtml(state.discard_top);
    } else {
        els.discardBtn.className = "playing-card pile-card empty-card";
        els.discardBtn.textContent = "EMPTY";
    }

    requestAnimationFrame(layoutArLabels);
}

/** Place draw/take/play as screen-flat HUD text just above each pile card. */
function layoutArLabels() {
    const stage = document.querySelector(".table-stage");
    if (!stage || !els.drawPrompt) return;
    const stageRect = stage.getBoundingClientRect();
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
        btn.style.left = `${((cx - stageRect.left) / stageRect.width) * 100}%`;
        btn.style.top = `${((top - stageRect.top) / stageRect.height) * 100}%`;
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
 * CSS owns perspective + rotateX. Seats only translate + rotateZ (flat on felt).
 * Yaw is computed from FINAL left/top in untransformed surface pixels — not from azimuth.
 */
const TABLE = {
    rim: 0.5,
    tiltDeg: 20,
    perspectivePx: 1800,
};

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

function applyTableCameraSettings() {
    const table3d = document.querySelector(".table-3d");
    if (!table3d) return;
    table3d.style.setProperty("--table-tilt", `${TABLE.tiltDeg}deg`);
    table3d.style.setProperty("--table-perspective", `${TABLE.perspectivePx}px`);
}

/**
 * CSS rotateZ whose local "top" points from the seat toward table center.
 * Uses clientWidth/Height (unprojected), never getBoundingClientRect().
 */
function yawTowardTableCenter(leftPct, topPct, surfaceElement) {
    const width = surfaceElement?.clientWidth || 0;
    const height = surfaceElement?.clientHeight || 0;
    if (!width || !height) return 0;
    const dx = ((50 - leftPct) / 100) * width;
    const dy = ((50 - topPct) / 100) * height;
    // Local seat top starts as (0, -1). Positive CSS rotateZ is clockwise.
    return shortestDeg((Math.atan2(dx, -dy) * 180) / Math.PI);
}

function clampSeatLeft(left) {
    return Math.max(24, Math.min(76, left));
}

function clampSeatTop(top, isLocal) {
    let t = Math.max(24, Math.min(68, top));
    if (isLocal) t = Math.min(t, 66);
    return t;
}

/** Density scale by player count only — never by camera depth. */
function densityScale(count, isLocal) {
    if (isLocal) return count >= 5 ? 1.0 : 1.03;
    if (count >= 6) return 0.84;
    if (count >= 5) return 0.86;
    if (count >= 4) return 0.9;
    return 0.92;
}

function seatLayout(count) {
    const n = Math.max(2, Math.min(6, count || 2));
    const azList = SEAT_AZIMUTHS[n] || SEAT_AZIMUTHS[4];
    const surface = document.querySelector(".table-surface");

    return azList.map((azimuthDeg, i) => {
        const isLocal = i === 0;
        const phi = ((90 + azimuthDeg) * Math.PI) / 180;

        // Percentage ellipse (CSS box aspect creates the visual oval).
        let left = 50 + TABLE.rim * Math.cos(phi) * 50;
        let top = 50 + TABLE.rim * Math.sin(phi) * 50;

        // Extra inset so 2x2 boards clear the wood rim.
        left = 50 + (left - 50) * 0.9;
        top = 50 + (top - 50) * 0.88;

        left = clampSeatLeft(left);
        top = clampSeatTop(top, isLocal);
        if (isLocal) left = 50;

        // Yaw only after final position — matches real W/H pixels.
        const yaw = yawTowardTableCenter(left, top, surface);

        return {
            left,
            top,
            yaw,
            scale: densityScale(n, isLocal),
            isLocal,
        };
    });
}

function renderSeats(options = {}) {
    applyTableCameraSettings();
    const order = rotatedOrder();
    const positions = seatLayout(order.length);
    els.seats.innerHTML = order
        .map((sid, index) => renderSeat(sid, positions[index], options))
        .join("");
    els.seats.dataset.count = String(order.length);
    renderNameplates(order, positions);
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
        const badges = [
            player.called ? `<span class="badge yellow">CALLED</span>` : "",
            player.protected ? `<span class="badge yellow">SAFE</span>` : "",
            `<span class="badge blue turn-badge${sid === state.current_turn_sid ? "" : " is-off"}">TURN</span>`,
            player.is_bot ? `<span class="badge green">BOT</span>` : "",
            !player.connected ? `<span class="badge">OFF</span>` : "",
        ].join("");
        return `
            <div class="${classes.join(" ")}" data-nameplate="${sid}" style="left:${left.toFixed(2)}%;top:${top.toFixed(2)}%">
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
    const felt = document.querySelector(".felt");
    if (!stage || !els.nameplates || !felt) return;
    const stageRect = stage.getBoundingClientRect();
    const plateBox = els.nameplates.getBoundingClientRect();
    const feltRect = felt.getBoundingClientRect();
    if (stageRect.width < 8 || stageRect.height < 8 || plateBox.width < 8) return;

    const ecx = (feltRect.left + feltRect.right) / 2;
    const ecy = (feltRect.top + feltRect.bottom) / 2;
    const erx = feltRect.width / 2;
    const ery = feltRect.height / 2;

    order.forEach((sid) => {
        const plate = els.nameplates.querySelector(`[data-nameplate="${CSS.escape(sid)}"]`);
        const seat = document.querySelector(`.seat[data-sid="${CSS.escape(sid)}"]`);
        if (!plate || !seat) return;
        const r = seat.getBoundingClientRect();
        const sx = (r.left + r.right) / 2;
        const sy = (r.top + r.bottom) / 2;
        let dx = sx - ecx;
        let dy = sy - ecy;
        const len = Math.hypot(dx, dy) || 1;
        dx /= len;
        dy /= len;
        // Distance from center to ellipse edge along this ray (screen-space).
        const tEdge = 1 / Math.sqrt((dx / erx) ** 2 + (dy / ery) ** 2);
        const platePad = Math.max(plate.offsetWidth, plate.offsetHeight) * 0.55 + 18;
        const x = ecx + dx * (tEdge + platePad);
        const y = ecy + dy * (tEdge + platePad);
        plate.style.left = `${(((x - plateBox.left) / plateBox.width) * 100).toFixed(2)}%`;
        plate.style.top = `${(((y - plateBox.top) / plateBox.height) * 100).toFixed(2)}%`;
    });
}

function renderSeat(sid, position, options = {}) {
    const player = state.players[sid];
    const isMe = sid === mySid;
    const classes = ["seat"];
    if (isMe) classes.push("me");
    if (player.protected) classes.push("protected");
    if (sid === state.current_turn_sid) classes.push("current-turn");

    const hideHeld = options.hideAnimTargets && options.action
        && (options.action.type === "draw" || options.action.type === "take")
        && options.action.sid === sid;

    const hideSlot = options.hideAnimTargets && options.action
        && options.action.type === "swap"
        && options.action.sid === sid
        ? options.action.index
        : -1;

    const held = renderHeldSlot(sid, { hidden: hideHeld });
    const cols = player.board.length <= 4 ? 2 : Math.min(3, Math.ceil(player.board.length / 2));
    const cards = player.board
        .map((slot, index) => renderBoardCard(sid, index, slot, { hidden: index === hideSlot }))
        .join("");

    const style = [
        `--seat-left:${position.left.toFixed(2)}%`,
        `--seat-top:${position.top.toFixed(2)}%`,
        `--seat-yaw:${position.yaw.toFixed(1)}deg`,
        `--seat-scale:${position.scale.toFixed(3)}`,
        // Local-space offset → toward viewer after rotateZ(yaw)
        `--shadow-x:${(Math.sin((position.yaw * Math.PI) / 180) * 7).toFixed(1)}px`,
        `--shadow-y:${(Math.cos((position.yaw * Math.PI) / 180) * 8).toFixed(1)}px`,
    ].join(";");

    return `
        <section class="${classes.join(" ")}" style="${style}" data-sid="${sid}">
            <div class="seat-scale">
                <div class="seat-board">
                    ${held}
                    <div class="card-grid cols-${cols}" data-grid="${sid}">${cards}</div>
                </div>
            </div>
        </section>
    `;
}

function renderHeldSlot(sid, options = {}) {
    const isMe = sid === mySid;
    const holding = playerIsHolding(sid);
    const peek = state.held_peek?.sid === sid ? state.held_peek : null;

    // Local player always shows a distinct hand tray to the left of the grid.
    if (!holding && !peek) {
        if (isMe) {
            return `<div class="held-slot hand-tray empty-tray" data-held="${sid}"><span class="hand-label">hand</span></div>`;
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
        <div class="held-slot filled${trayClass}${hiddenClass}" data-held="${sid}">
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
    const burnt = isBurntSlot(ownerSid, index);
    const highlight = shouldHighlightSlot(ownerSid, index);
    const opening = canOpeningPeek(ownerSid, index, slot);
    const classes = ["board-card", slot.faceUp ? "face-up" : "face-down"];
    if (slot.faceUp && slot.card) classes.push(colorClass(slot.card));
    if (selected) classes.push("selected");
    if (burnt) classes.push("burnt");
    if (highlight) classes.push("swap-mark");
    if (opening) classes.push("opening-peek");
    if (options.hidden) classes.push("anim-hidden");
    if (isClickable(ownerSid, index, slot) || opening) classes.push("selectable");

    const html = slot.faceUp && slot.card
        ? cardFaceHtml(slot.card)
        : "";

    return `<button type="button" class="${classes.join(" ")}" data-owner="${ownerSid}" data-index="${index}">${html}</button>`;
}

function canOpeningPeek(ownerSid, index, slot) {
    if (ownerSid !== mySid || slot.empty || slot.faceUp) return false;
    const me = state.players[mySid];
    if (!me?.opening_peekable) return false;
    return index === 2 || index === 3;
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
                return !(ownerSid !== mySid && state.players[ownerSid].protected);
            }
        }
        return false;
    }

    if (holdingMyDraw()) {
        return ownerSid === mySid;
    }

    if (state.status === "playing" && state.discard_top && !state.pending_burn && !holdingMyDraw()) {
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

    const slot = state.players[ownerSid]?.board?.[index];
    if (slot && canOpeningPeek(ownerSid, index, slot)) {
        socket.emit("peek_opening", { room: ROOM_ID, index });
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

    if (state.status === "playing" && state.discard_top && !state.pending_burn && !holdingMyDraw()) {
        socket.emit("burn_card", { room: ROOM_ID, owner_sid: ownerSid, index });
        return;
    }

    if (state.status === "playing" && !state.discard_top) {
        showToast("Nothing to burn against — discard is empty.");
    }
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
    if (ability?.sid === mySid && ability.stage === "deciding" && ability.peek_pair) {
        const cards = ability.peek_pair.map((item) => `
            <div class="peek-pair-item">
                <div class="playing-card mini-card ${colorClass(item.card)}">${cardFaceHtml(item.card)}</div>
                <p>${escapeHtml(state.players[item.owner_sid]?.username || "")} #${item.index + 1}</p>
                ${item.burnable ? `<button type="button" class="btn btn-purple tiny-btn" onclick="burnCard('${item.owner_sid}', ${item.index})">burn</button>` : ""}
            </div>
        `).join("");
        els.abilityOverlay.classList.remove("hidden");
        els.abilityOverlay.innerHTML = `
            <div class="overlay-card">
                <h2>Black King</h2>
                <div class="peek-pair-row">${cards}</div>
                <div class="panel-actions">
                    <button class="btn btn-green" onclick="blackKingDecision(true)">Swap</button>
                    <button class="btn btn-gray" onclick="blackKingDecision(false)">Keep</button>
                </div>
            </div>
        `;
        return;
    }

    if (ability?.sid === mySid && ability.stage === "selecting") {
        let prompt = "Select a card.";
        if (ability.type === "peek_own") prompt = "Select one of your cards to peek.";
        if (ability.type === "peek_other") prompt = "Select an opponent's card to peek.";
        if (ability.type === "switch_unseen") prompt = `Select two cards to switch unseen (${ability.selected.length}/2).`;
        if (ability.type === "switch_peek") prompt = `Select two cards to inspect (${ability.selected.length}/2).`;
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
    } else if (state.players[mySid]?.opening_peekable) {
        text = "Click your two bottom cards to peek, then draw when ready.";
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
        text = "Draw, take discard, or call. Click any board card to attempt a burn.";
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
        const raw = state.round_results?.raw_scores?.[sid] ?? 0;
        const round = state.round_results?.round_scores?.[sid] ?? 0;
        return `<div class="result-row"><span>${escapeHtml(player.username)}</span><strong>hand ${raw} / +${round}</strong></div>`;
    }).join("");
    const loserText = state.status === "game_over" && state.winner_summary
        ? `<p>${state.winner_summary.losers.map((sid) => escapeHtml(state.players[sid].username)).join(", ")} hit ${state.winner_summary.target_score}+ and loses.</p>`
        : `<p>${escapeHtml(state.players[state.round_results?.next_start_sid]?.username || "Next")} starts next.</p>`;
    return `
        <div class="overlay-card">
            <h2>${state.status === "game_over" ? "Game over" : "Round over"}</h2>
            ${loserText}
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

function captureAnchors() {
    const rect = (el, yawEl = null) => {
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
        };
    };
    const out = {
        draw: rect(els.drawBtn),
        discard: rect(els.discardBtn),
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
            seat
        );
        out.boards[sid] = {};
        const len = state.players[sid]?.board?.length || 4;
        for (let i = 0; i < len; i++) {
            out.boards[sid][i] = rect(
                document.querySelector(`.board-card[data-owner="${CSS.escape(sid)}"][data-index="${i}"]`),
                seat
            );
        }
        out.seats[sid] = rect(seat, seat);
    }
    return out;
}

function captureFullAnchors() {
    return captureAnchors();
}

function flyCard({ from, to, html, className = "", duration = ANIM_MS }) {
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
                el.remove();
                resolve();
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
    if (slot && slot.w >= 30 && slot.h >= 40) return slot;
    return destHeld(sid, afterAnchors);
}

async function playActionAnimation(action, beforeAnchors) {
    animating = true;
    const safety = setTimeout(() => { animating = false; }, Math.max(ANIM_MS, 2000) + 500);
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
            await flyCard({ from: srcDraw, to, html });
        } else if (action.type === "take") {
            const to = destHeld(action.sid, after);
            await flyCard({
                from: srcDiscard,
                to,
                html: action.sid === mySid && action.card ? faceUpHtml(action.card) : faceDownHtml(),
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
                }),
                flyCard({
                    from: slotFrom || slotTo,
                    to: discardTo,
                    html: faceUpHtml(action.outgoing),
                }),
            ]);
        } else if (action.type === "play") {
            const from = (before.held && before.held[action.sid]) || after.held[action.sid] || after.seats[action.sid] || srcDraw;
            await flyCard({
                from,
                to: after.discard || srcDiscard,
                html: faceUpHtml(action.card),
            });
        } else if (action.type === "burn") {
            const from = (before.boards && before.boards[action.owner_sid] && before.boards[action.owner_sid][action.index])
                || destBoard(action.owner_sid, action.index, after);
            await flyCard({
                from,
                to: after.discard || srcDiscard,
                html: faceUpHtml(action.card),
            });
        } else if (action.type === "burn_fail") {
            // Missed burn stays face-up on the board so everyone can see it.
            await wait(1800);
        } else if (action.type === "switch") {
            const a = action.a;
            const b = action.b;
            if (a && b) {
                const fromA = destBoard(a.owner_sid, a.index, after);
                const fromB = destBoard(b.owner_sid, b.index, after);
                await Promise.all([
                    flyCard({ from: fromA, to: fromB, html: faceDownHtml() }),
                    flyCard({ from: fromB, to: fromA, html: faceDownHtml() }),
                ]);
            }
        } else if (action.type === "peek") {
            const from = destBoard(action.owner_sid, action.index, after);
            const to = destHeld(action.sid, after);
            await flyCard({ from, to, html: faceDownHtml() });
        } else if (action.type === "put_back") {
            const from = destHeld(action.sid, after);
            const to = destBoard(action.owner_sid, action.index, after);
            await flyCard({ from, to, html: faceDownHtml() });
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
    socket.emit("burn_card", { room: ROOM_ID, owner_sid: ownerSid, index });
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
