/*
 * Bot brain profiles.
 *
 * The server remains the rules referee, but every timing and strategy choice
 * comes from this file. Values are deliberately readable so bot personality
 * can be tuned without touching multiplayer game rules.
 */
const BOT_BRAINS = Object.freeze({
    easy: Object.freeze({
        reaction: [3.2, 5.2],
        mistake: 0.35,
        randomSwap: 0.28,
        swapGain: 6,
        discardGain: 5,
        takeLowValue: 1,
        takeLowMinGain: 1,
        abilityRate: 0.25,
        peekBurnRate: 0.25,
        callScore: -1,
        callRate: 0.45,
        callCardCount: 0,
        callCardScore: -1,
        finalCallScore: -1,
        finalCallRate: 0.35,
        burnOwnMin: 9,
        burnOpponentGain: 99,
        switchRandomRate: 1,
        switchExecuteRate: 0.55,
        switchOwnMin: 99,
        switchTargetLowest: 0,
    }),
    medium: Object.freeze({
        reaction: [1.6, 2.8],
        mistake: 0.05,
        randomSwap: 0.03,
        swapGain: 1,
        discardGain: 1,
        takeLowValue: 0,
        takeLowMinGain: 0,
        abilityRate: 0.88,
        peekBurnRate: 0.9,
        callScore: 2,
        callRate: 1,
        callCardCount: 2,
        callCardScore: 4,
        finalCallScore: 7,
        finalCallRate: 1,
        burnOwnMin: 3,
        burnOpponentGain: 2,
        switchRandomRate: 0,
        switchExecuteRate: 1,
        switchOwnMin: 5,
        switchTargetLowest: 0.72,
    }),
    hard: Object.freeze({
        reaction: [1.6, 3.0],
        mistake: 0.03,
        randomSwap: 0.01,
        swapGain: 1,
        discardGain: 1,
        takeLowValue: 0,
        takeLowMinGain: 0,
        abilityRate: 0.95,
        peekBurnRate: 0.98,
        callScore: 0,
        callRate: 1,
        callCardCount: 3,
        callCardScore: 3,
        finalCallScore: 7,
        finalCallRate: 1,
        burnOwnMin: 1,
        burnOpponentGain: 0,
        switchRandomRate: 0,
        switchExecuteRate: 1,
        switchOwnMin: -2,
        switchTargetLowest: 1,
    }),
});

function botPolicyFor(difficulty) {
    const brain = BOT_BRAINS[difficulty] || BOT_BRAINS.medium;
    return {
        reaction: [...brain.reaction],
        mistake: brain.mistake,
        random_swap: brain.randomSwap,
        swap_gain: brain.swapGain,
        discard_gain: brain.discardGain,
        take_low_value: brain.takeLowValue,
        take_low_min_gain: brain.takeLowMinGain,
        ability_rate: brain.abilityRate,
        peek_burn_rate: brain.peekBurnRate,
        call_score: brain.callScore,
        call_rate: brain.callRate,
        call_card_count: brain.callCardCount,
        call_card_score: brain.callCardScore,
        final_call_score: brain.finalCallScore,
        final_call_rate: brain.finalCallRate,
        burn_own_min: brain.burnOwnMin,
        burn_opponent_gain: brain.burnOpponentGain,
        switch_random_rate: brain.switchRandomRate,
        switch_execute_rate: brain.switchExecuteRate,
        switch_own_min: brain.switchOwnMin,
        switch_target_lowest: brain.switchTargetLowest,
    };
}

function initializeGameMode() {
    joinGame("You", {
        botMode: true,
        botCount: BOT_COUNT,
        botDifficulty: BOT_DIFFICULTY,
        botPolicy: botPolicyFor(BOT_DIFFICULTY),
    });
}
