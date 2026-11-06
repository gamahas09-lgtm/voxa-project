const express = require('express');
const http = http = require('http'); // تم تصحيح طريقة استدعاء http
const { Server } = require('socket.io');
const path = require('path');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: { origin: "*" } // السماح بالاتصال من أي مكان
});

// تقديم ملف الـ HTML الخاص بتسجيل الدخول كصفحة رئيسية
app.get('/', (req, res) => {
    // استبدل 'login.html' باسم ملف صفحة تسجيل الدخول لديك
    res.sendFile(path.join(__dirname, 'login.html'));
});

// لو أردت الاحتفاظ بملف index.html في مسار آخر مثل /app أو /chat
app.get('/chat', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

// لما حد يتصل عبر Socket.io
io.on('connection', (socket) => {
    console.log('مستخدم جديد اتصل:', socket.id);

    socket.on('join', (roomID) => {
        socket.join(roomID);
        socket.to(roomID).emit('user-joined', socket.id);
    });

    socket.on('signal', (data) => {
        io.to(data.to).emit('signal', {
            from: socket.id,
            signal: data.signal
        });
    });

    socket.on('disconnect', () => {
        console.log('مستخدم خرج:', socket.id);
        socket.broadcast.emit('user-left', socket.id);
    });
});

const PORT = process.env.PORT || 8000; // متوافق تماماً مع منصة Railway
server.listen(PORT, () => {
    console.log(`السيرفر شغال على البورت ${PORT}`);
});