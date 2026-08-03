package com.example.quotes;

import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.server.ServerRequest;
import org.springframework.web.reactive.function.server.ServerResponse;
import reactor.core.publisher.Mono;

/** Functional-style handlers. */
@Component
public class WatchlistHandler {

    /** List all watchlists. */
    public Mono<ServerResponse> list(ServerRequest request) {
        return ServerResponse.ok().body(Mono.empty(), Watchlist.class);
    }

    /** Fetch one watchlist. */
    public Mono<ServerResponse> get(ServerRequest request) {
        return ServerResponse.ok().body(Mono.empty(), Watchlist.class);
    }

    /** Create a watchlist. */
    public Mono<ServerResponse> create(ServerRequest request) {
        return ServerResponse.ok().body(Mono.empty(), Watchlist.class);
    }
}
