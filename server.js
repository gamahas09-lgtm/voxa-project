const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const path = require('path');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: { origin: "*" } // عشان نسمح بالاتصال من أي مكان
});

// تقديم ملف الـ HTML
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

// لما حد يتصل
io.on('connection', (socket) => {
    console.log('مستخدم جديد اتصل:', socket.id);

    // استقبال الـ ID وعمل ربط بين الاتنين
    socket.on('join', (roomID) => {
        socket.join(roomID);
        socket.to(roomID).emit('user-joined', socket.id);
    });

    // تبادل إشارات الاتصال بين المتصفحين (Signaling)
    socket.on('signal', (data) => {
        io.to(data.to).emit('signal', {
            from: socket.id,
            signal: data.signal
        });
    });

    // لو حد خرج
    socket.on('disconnect', () => {
        console.log('مستخدم خرج:', socket.id);
        socket.broadcast.emit('user-left', socket.id);
    });
});

const PORT = 8000;
server.listen(PORT, () => {
    console.log(`السيرفر شغال على http://localhost:${PORT}`);
});