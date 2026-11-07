/* Calendar-month addition. Matches the server: clamp to the last real day. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.NetflixDates = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function addOneMonthISO(iso) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!match) return null;
    var year = Number(match[1]);
    var month = Number(match[2]);
    var day = Number(match[3]);
    if (month < 1 || month > 12 || day < 1 || day > 31) return null;
    month += 1;
    if (month > 12) {
      month = 1;
      year += 1;
    }
    var lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
    var clamped = Math.min(day, lastDay);
    var mm = String(month).padStart(2, "0");
    var dd = String(clamped).padStart(2, "0");
    return year + "-" + mm + "-" + dd;
  }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function formatCalendarDate(iso) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!match) return null;
    var month = Number(match[2]);
    var day = Number(match[3]);
    if (month < 1 || month > 12 || day < 1 || day > 31) return null;
    return match[3] + " " + MONTHS[month - 1] + " " + match[1];
  }

  // Date-only business clock. Do not use toISOString() or new Date("YYYY-MM-DD"):
  // both are UTC and shift the calendar day between 00:00 and 05:30 in India.
  function businessTodayISO(now) {
    var parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).formatToParts(now || new Date());
    var bag = {};
    parts.forEach(function (part) { bag[part.type] = part.value; });
    return bag.year + "-" + bag.month + "-" + bag.day;
  }

  return {
    addOneMonthISO: addOneMonthISO,
    formatCalendarDate: formatCalendarDate,
    businessTodayISO: businessTodayISO
  };
});
