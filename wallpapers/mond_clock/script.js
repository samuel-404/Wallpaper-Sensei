function updateClock() {
    const now = new Date();
    
    // Day
    const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    document.getElementById('day').textContent = days[now.getDay()];

    // Date
    const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
    const dateStr = `${months[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
    document.getElementById('date').textContent = dateStr;

    // Time
    let hours = now.getHours();
    const minutes = now.getMinutes();
    const ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12;
    hours = hours ? hours : 12; // the hour '0' should be '12'
    const minutesStr = minutes < 10 ? '0' + minutes : minutes;
    const timeStr = `- ${hours}:${minutesStr} ${ampm} -`;
    document.getElementById('time').textContent = timeStr;
}

// Initial update
updateClock();

// Sync to next minute
const now = new Date();
const msUntilNextMinute = (60 - now.getSeconds()) * 1000 - now.getMilliseconds();

setTimeout(() => {
    updateClock();
    setInterval(updateClock, 60000);
}, msUntilNextMinute);
