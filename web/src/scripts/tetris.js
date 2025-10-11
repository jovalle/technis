const canvas = document.getElementById('tetris');
const ctx = canvas.getContext('2d');
const textOverlay = document.getElementById('text-overlay');

// Set canvas size
const BLOCK_SIZE = 30;
const COLS = 10;
const ROWS = 20;
canvas.width = COLS * BLOCK_SIZE;
canvas.height = ROWS * BLOCK_SIZE;

// Tetromino shapes with rotations (0 = spawn state, clockwise rotation)
const SHAPES = {
    I: [
        [[1, 1, 1, 1]],           // 0: horizontal
        [[1], [1], [1], [1]]      // 1: vertical
    ],
    O: [
        [[1, 1], [1, 1]],         // 0: square (all rotations same)
        [[1, 1], [1, 1]]          // 1: square
    ],
    T: [
        [[0, 1, 0], [1, 1, 1]],   // 0: T pointing up
        [[1, 0], [1, 1], [1, 0]], // 1: T pointing right
        [[1, 1, 1], [0, 1, 0]],   // 2: T pointing down
        [[0, 1], [1, 1], [0, 1]]  // 3: T pointing left
    ],
    S: [
        [[0, 1, 1], [1, 1, 0]],   // 0: horizontal S
        [[1, 0], [1, 1], [0, 1]]  // 1: vertical S
    ],
    Z: [
        [[1, 1, 0], [0, 1, 1]],   // 0: horizontal Z
        [[0, 1], [1, 1], [1, 0]]  // 1: vertical Z
    ],
    J: [
        [[1, 0, 0], [1, 1, 1]]    // 0: J with hook on left
    ],
    L: [
        [[0, 0, 1], [1, 1, 1]]    // 0: L with hook on right
    ]
};

let currentRotation = 0;

// Game state
let board = Array(ROWS).fill(null).map(() => Array(COLS).fill(0));
let currentPiece = null;
let pieceX = 0;
let pieceY = 0;
let dropSpeed = 80; // Fast drop
let lastDrop = 0;
let animationState = 'dropping'; // dropping, clearing, fading, showing_logo, logo_waiting, logo_fading, resetting
let fadeAlpha = 1.0;
let logoAlpha = 0.0;
let textTimer = 0;
let clearTimer = 0;
let skipToLogo = false;
let paused = false;

// Exact sequence of pieces that build 2 rows with a T-shaped gap
const dropSequence = [
    { shape: 'L', x: 6, rotation: 0 },
    { shape: 'J', x: 2, rotation: 0 },
    { shape: 'O', x: 0, rotation: 0 },
    { shape: 'Z', x: 7, rotation: 1 },
    { shape: 'S', x: 2, rotation: 1 },
    { shape: 'I', x: 9, rotation: 1 },
    { shape: 'O', x: 0, rotation: 1 },
    { shape: 'T', x: 4, rotation: 2 }  // Final T piece pointing down
];
let sequenceIndex = 0;

function drawBlock(x, y, isTetromino = false) {
    ctx.fillStyle = `rgba(255, 255, 255, ${fadeAlpha})`;
    ctx.fillRect(x * BLOCK_SIZE + 1, y * BLOCK_SIZE + 1, BLOCK_SIZE - 2, BLOCK_SIZE - 2);
}

function drawBoard() {
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Draw placed pieces with current fade alpha
    for (let y = 0; y < ROWS; y++) {
        for (let x = 0; x < COLS; x++) {
            if (board[y][x]) {
                drawBlock(x, y);
            }
        }
    }
}

function drawPiece() {
    if (!currentPiece) return;
    const shape = SHAPES[currentPiece][currentRotation];
    for (let y = 0; y < shape.length; y++) {
        for (let x = 0; x < shape[y].length; x++) {
            if (shape[y][x]) {
                drawBlock(pieceX + x, pieceY + y, true);
            }
        }
    }
}

function collision() {
    const shape = SHAPES[currentPiece][currentRotation];
    for (let y = 0; y < shape.length; y++) {
        for (let x = 0; x < shape[y].length; x++) {
            if (shape[y][x]) {
                const newX = pieceX + x;
                const newY = pieceY + y;
                if (newX < 0 || newX >= COLS || newY >= ROWS) {
                    return true;
                }
                if (newY >= 0 && board[newY][newX]) {
                    return true;
                }
            }
        }
    }
    return false;
}

function merge() {
    const shape = SHAPES[currentPiece][currentRotation];
    for (let y = 0; y < shape.length; y++) {
        for (let x = 0; x < shape[y].length; x++) {
            if (shape[y][x]) {
                const boardY = pieceY + y;
                const boardX = pieceX + x;
                if (boardY >= 0) {
                    board[boardY][boardX] = 1;
                }
            }
        }
    }
}

function spawnPiece() {
    if (sequenceIndex >= dropSequence.length) {
        // All pieces dropped, wait a moment then check for line clears
        animationState = 'clearing';
        clearTimer = 0;
        return;
    }

    const nextPiece = dropSequence[sequenceIndex];
    currentPiece = nextPiece.shape;
    currentRotation = nextPiece.rotation || 0;
    pieceX = nextPiece.x;
    pieceY = -4;
    sequenceIndex++;
}

function checkLines() {
    const lines = [];
    for (let y = ROWS - 1; y >= 0; y--) {
        if (board[y].every(cell => cell === 1)) {
            lines.push(y);
        }
    }
    return lines;
}

function clearLines(lines) {
    // Remove cleared lines
    for (let i = lines.length - 1; i >= 0; i--) {
        board.splice(lines[i], 1);
        board.unshift(Array(COLS).fill(0));
    }
}

function showLogo() {
    textOverlay.classList.remove('hidden', 'fade-out');
    textOverlay.classList.add('visible', 'fade-in');
}

function hideLogo() {
    textOverlay.classList.remove('fade-in');
    textOverlay.classList.add('fade-out');
}

function resetLogo() {
    textOverlay.classList.remove('visible', 'fade-in', 'fade-out');
    textOverlay.classList.add('hidden');
}

function reset() {
    board = Array(ROWS).fill(null).map(() => Array(COLS).fill(0));
    currentPiece = null;
    currentRotation = 0;
    sequenceIndex = 0;
    fadeAlpha = 1.0;
    logoAlpha = 0.0;
    animationState = 'dropping';
    resetLogo();
}

function update(timestamp) {
    // Handle skip to logo
    if (skipToLogo && !paused) {
        // Clear the board and fade to logo immediately
        fadeAlpha -= 0.1;
        if (fadeAlpha <= 0) {
            fadeAlpha = 0;
            board = Array(ROWS).fill(null).map(() => Array(COLS).fill(0));
            currentPiece = null;
            animationState = 'showing_logo';
            skipToLogo = false;
            paused = true;
            showLogo();
        }
    } else if (!paused) {
        if (animationState === 'dropping') {
            if (timestamp - lastDrop > dropSpeed) {
                if (!currentPiece) {
                    spawnPiece();
                } else {
                    pieceY++;
                    if (collision()) {
                        pieceY--;
                        merge();
                        currentPiece = null;
                    }
                }
                lastDrop = timestamp;
            }
        } else if (animationState === 'clearing') {
            // Brief pause before clearing lines
            if (clearTimer === 0) {
                clearTimer = timestamp;
            }
            if (timestamp - clearTimer > 300) {
                const lines = checkLines();
                if (lines.length > 0) {
                    clearLines(lines);
                }
                animationState = 'fading';
                textTimer = timestamp;
            }
        } else if (animationState === 'fading') {
            // Fade out the tetris blocks
            fadeAlpha -= 0.03;
            if (fadeAlpha <= 0) {
                fadeAlpha = 0;
                animationState = 'showing_logo';
                textTimer = timestamp;
                showLogo();
            }
        } else if (animationState === 'showing_logo') {
            // Logo stays visible (paused state - no timeout)
            // Animation stops here when clicked
        } else if (animationState === 'logo_fading') {
            // Wait for fade out to complete (1 second)
            if (timestamp - textTimer > 1000) {
                animationState = 'resetting';
                textTimer = timestamp;
            }
        } else if (animationState === 'resetting') {
            // Brief pause before restarting
            if (timestamp - textTimer > 500) {
                reset();
            }
        }
    }

    drawBoard();
    drawPiece();
    requestAnimationFrame(update);
}

// Click to skip to logo
document.addEventListener('click', () => {
    if (!paused && animationState !== 'showing_logo') {
        skipToLogo = true;
    }
});

// Start animation
requestAnimationFrame(update);
