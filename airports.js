/* Location labels are presentation; only catalog IATA codes enter requests. */
(function (root) {
  function normalize(value) {
    return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  }
  function label(place) { return `${place.city || place.name} (${place.code})`; }
  function editDistance(a, b) {
    let previous = Array.from({length: b.length + 1}, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const next = [i];
      for (let j = 1; j <= b.length; j++) next[j] = Math.min(next[j - 1] + 1,
        previous[j] + 1, previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      previous = next;
    }
    return previous[b.length];
  }
  function createCatalog(places) {
    const entries = places.map(place => ({place, code: normalize(place.code),
      city: normalize(place.city), name: normalize(place.name),
      text: normalize([place.code, place.city, place.name, place.country, place.label, ...(place.aliases || [])].join(' '))}));
    const byCode = new Map(entries.map(entry => [entry.code, entry.place]));
    const byLabel = new Map(entries.map(entry => [normalize(label(entry.place)), entry.place]));
    function search(value, limit = 8) {
      const query = normalize(value);
      if (!query) return [];
      const tokens = query.split(' ');
      const scored = entries.map(entry => {
        let score = Infinity;
        if (entry.code === query) score = 0;
        else if (entry.place.type === 'city' && (entry.city === query || entry.name === query)) score = 10;
        else if (entry.code.startsWith(query)) score = 20;
        else if (entry.city.startsWith(query) || entry.name.startsWith(query)) score = 30;
        else if (tokens.every(token => entry.text.split(' ').some(word => word.startsWith(token)))) score = 40;
        else if (tokens.every(token => entry.text.includes(token))) score = 50;
        else if (query.length >= 4 && !query.includes(' ')) {
          const distance = Math.min(...[entry.city, ...entry.name.split(' ')].filter(Boolean)
            .map(word => Math.abs(word.length - query.length) <= 2 ? editDistance(word, query) : Infinity));
          if (distance <= (query.length >= 7 ? 2 : 1)) score = 60 + distance;
        }
        const rank = {city: 0, large_airport: 1, medium_airport: 2, small_airport: 3}[entry.place.type] ?? 4;
        return {place: entry.place, score, rank};
      }).filter(result => Number.isFinite(result.score));
      const matches = scored.some(result => result.score < 60) ? scored.filter(result => result.score < 60) : scored;
      return matches.sort((a, b) => a.score - b.score || a.rank - b.rank || a.place.code.localeCompare(b.place.code))
        .slice(0, limit).map(result => result.place);
    }
    function resolve(value) {
      const query = normalize(value);
      if (!query) return null;
      const exact = byCode.get(query) || byLabel.get(query);
      if (exact) return exact;
      const matches = entries.filter(entry => entry.city === query || entry.name === query || (entry.place.aliases || []).some(alias => normalize(alias) === query));
      // Ambiguous names require a choice, including cities in different countries.
      if (matches.length === 1) return matches[0].place;
      return null;
    }
    return {search, resolve};
  }
  root.AirportSearch = {normalize, label, createCatalog};
  if (typeof module !== 'undefined') module.exports = root.AirportSearch;
})(typeof globalThis !== 'undefined' ? globalThis : window);
