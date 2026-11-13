'use strict';
const { contextBridge, ipcRenderer } = require('electron');

// No generic IPC, filesystem access or credential values cross this bridge.
contextBridge.exposeInMainWorld('ttsDesktop', {
  minimize: () => ipcRenderer.invoke('desktop:action', 'minimize'),
  toggleMaximize: () => ipcRenderer.invoke('desktop:action', 'toggleMaximize'),
  close: () => ipcRenderer.invoke('desktop:action', 'close'),
  getWindowState: () => ipcRenderer.invoke('desktop:action', 'getWindowState'),
  editCredentials: () => ipcRenderer.invoke('desktop:action', 'editCredentials'),
  restart: () => ipcRenderer.invoke('desktop:action', 'restart'),
  openDataDirectory: () => ipcRenderer.invoke('desktop:action', 'openDataDirectory'),
  onWindowState: callback => {
    if (typeof callback !== 'function') return () => {};
    const listener = (_event, state) => callback({maximized: !!state.maximized});
    ipcRenderer.on('desktop:window-state', listener);
    return () => ipcRenderer.removeListener('desktop:window-state', listener);
  },
});
