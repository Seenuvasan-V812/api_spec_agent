package com.example.quotes;

import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/** Annotated reactive endpoints. */
@RestController
public class QuoteController {

    /** Latest quote for a symbol. */
    @GetMapping("/quotes/{symbol}")
    public Mono<Quote> latest(@PathVariable String symbol) {
        return Mono.empty();
    }

    /** Stream quotes as server-sent events. */
    @GetMapping(value = "/quotes/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<Quote> stream() {
        return Flux.empty();
    }

    /** Publish a quote. */
    @PostMapping("/quotes")
    public Mono<Quote> publish(@RequestBody Quote quote) {
        return Mono.just(quote);
    }
}
