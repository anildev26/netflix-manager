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
  return { addOneMonthISO: addOneMonthISO };
});
