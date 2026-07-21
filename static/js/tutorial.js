const tutorialSteps = Array.from(document.querySelectorAll("[data-tutorial-step]"));
const tutorialDots = Array.from(document.querySelectorAll("[data-go-step]"));
const tutorialCounter = document.getElementById("tutorial-counter");
const tutorialPrev = document.getElementById("tutorial-prev");
const tutorialNext = document.getElementById("tutorial-next");
let tutorialIndex = 0;
tutorialSteps.forEach((step) => { step.tabIndex = -1; });

function showTutorialStep(index) {
    tutorialIndex = Math.max(0, Math.min(tutorialSteps.length - 1, index));
    tutorialSteps.forEach((step, stepIndex) => {
        step.classList.toggle("active", stepIndex === tutorialIndex);
    });
    tutorialDots.forEach((dot, dotIndex) => {
        const active = dotIndex === tutorialIndex;
        dot.classList.toggle("active", active);
        dot.classList.toggle("complete", dotIndex < tutorialIndex);
        dot.setAttribute("aria-current", active ? "step" : "false");
    });
    tutorialCounter.textContent = `${tutorialIndex + 1} / ${tutorialSteps.length}`;
    tutorialPrev.disabled = tutorialIndex === 0;
    tutorialNext.textContent = tutorialIndex === tutorialSteps.length - 1 ? "PLAY NOW" : "NEXT";
    tutorialSteps[tutorialIndex].focus({ preventScroll: true });
}

tutorialPrev.addEventListener("click", () => showTutorialStep(tutorialIndex - 1));
tutorialNext.addEventListener("click", () => {
    if (tutorialIndex === tutorialSteps.length - 1) {
        window.location.href = "/create-room";
        return;
    }
    showTutorialStep(tutorialIndex + 1);
});

tutorialDots.forEach((dot) => {
    dot.addEventListener("click", () => showTutorialStep(Number(dot.dataset.goStep)));
});

const choiceLessons = {
    draw: "Draw a hidden card. You may swap it into your grid or play it to the discard pile.",
    take: "Take the visible discard, then swap it with one of your own grid cards. Its special power does not activate.",
    call: "Call before taking another action. You become protected, and the other players receive their final turns.",
};

document.querySelectorAll("[data-choice]").forEach((button) => {
    button.addEventListener("click", () => {
        document.querySelectorAll("[data-choice]").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        document.getElementById("tutorial-choice-answer").textContent = choiceLessons[button.dataset.choice];
    });
});

document.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") showTutorialStep(tutorialIndex + 1);
    if (event.key === "ArrowLeft") showTutorialStep(tutorialIndex - 1);
});

showTutorialStep(0);
