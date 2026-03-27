#!/usr/bin/env node

/**
 * MiniDisc Batch Helper - Node.js Backend
 * Handles MiniDisc device communication for the PySide6 GUI
 *
 * This is a placeholder implementation. The actual MiniDisc device
 * communication would use the netmd-js library or similar.
 */

const readline = require('readline');

// Simulated device state
let deviceConnected = false;
let deviceName = 'Sony MZ-N510';

// Helper functions
function sendResponse(response) {
  console.log(JSON.stringify(response));
}

function handleCommand(command) {
  switch (command.action) {
    case 'get_device':
      sendResponse({
        connected: deviceConnected,
        name: deviceConnected ? deviceName : null
      });
      break;

    case 'upload_track':
      // Placeholder: In production, this would:
      // 1. Call audio converter if needed
      // 2. Upload to MiniDisc device via netmd-js
      sendResponse({
        success: true,
        message: `Uploaded ${command.path}`
      });
      break;

    case 'get_disc_info':
      sendResponse({
        used: 1800,  // seconds
        capacity: 4440,  // 74 minutes
        track_count: 5
      });
      break;

    case 'eject':
      sendResponse({ success: true });
      break;

    default:
      sendResponse({
        error: `Unknown action: ${command.action}`
      });
  }
}

// Read commands from stdin
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

let buffer = '';

rl.on('line', (line) => {
  buffer += line;

  try {
    const command = JSON.parse(buffer);
    handleCommand(command);
    buffer = '';
  } catch (e) {
    // Incomplete JSON, keep buffering
  }
});

// Handle stdin close
rl.on('close', () => {
  process.exit(0);
});

// Simulate device connection after 2 seconds
setTimeout(() => {
  deviceConnected = true;
  console.error('Device connected: ' + deviceName);
}, 2000);
