function initializeGameMode() {
    const savedUsername = savedRoomUsername();
    if (savedUsername) {
        joinGame(savedUsername);
        return;
    }
    const modal = document.createElement("div");
    modal.className = "modal-backdrop";
    modal.innerHTML = `
        <div class="name-modal">
            <h1>ENTER USERNAME</h1>
            <input id="username-input" type="text" placeholder="Your name..." autocomplete="off">
            <div class="panel-actions" style="justify-content: center; margin-top: 18px;">
                <button id="join-room-btn" class="btn btn-green">JOIN</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    const input = document.getElementById("username-input");
    const joinBtn = document.getElementById("join-room-btn");
    const join = () => {
        const username = input.value.trim() || `Player_${Math.floor(Math.random() * 1000)}`;
        modal.remove();
        joinGame(username);
    };

    joinBtn.addEventListener("click", join);
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") join();
    });
    input.focus();
}
