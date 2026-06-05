/**
 * BooksUp — surface book and journal links for the current article (from the
 * bup tool's API) and apply them so the editor can review/modify before saving.
 *
 * Works two ways:
 *   - Reading an article: click BooksUp -> pick suggestions -> opens the edit
 *     window with the changes applied.
 *   - Already editing wikitext: click BooksUp -> pick suggestions -> applies
 *     them straight into the edit box you're in (combine with other edits).
 *
 * The panel also has a "Find articles" section (most articles have no
 * suggestions), to find pages worth running it on:
 *   - Random: jump to a random article that has book links.
 *   - My watchlist: list your watchlisted articles that have book links.
 *   - Browse worklist: the full list at bup.toolforge.org.
 *
 * Install: copy to User:GreenC/BooksUp.js, then in User:GreenC/common.js add:
 *   mw.loader.load('//en.wikipedia.org/w/index.php?title=User:GreenC/BooksUp.js&action=raw&ctype=text/javascript');
 *
 * Source / issues: https://github.com/greencardamom/bup
 */
/* global mw, $ */
( function () {
	'use strict';

	// ---- config -----------------------------------------------------------
	var API = 'https://bup.toolforge.org/api/v1';
	var WEB = 'https://bup.toolforge.org';           // browsable worklist UI
	var DOC = 'User:GreenC/BooksUp';                 // linked in the edit summary
	var STASH_KEY = 'BooksUp.pending';               // read-page -> edit-page handoff
	var AUTORUN_KEY = 'BooksUp.autorun';             // auto-open on arrival (localStorage)

	function editSummary( n ) {
		return 'Adding book link' + ( n === 1 ? '' : 's' ) +
			' ([[' + DOC + '|BooksUp]])';
	}

	// ---- API / wiki access ------------------------------------------------

	function apiPageUrl( title ) {
		return API + '/page/' + encodeURIComponent( title ).replace( /%2F/gi, '/' );
	}

	function fetchCandidates( title ) {
		return fetch( apiPageUrl( title ), { headers: { Accept: 'application/json' } } )
			.then( function ( r ) {
				if ( r.status === 404 ) { return { found: false, citations: [] }; }
				if ( !r.ok ) { throw new Error( 'API HTTP ' + r.status ); }
				return r.json();
			} );
	}

	function fetchWikitext( title ) {
		return new mw.Api().get( {
			action: 'query', prop: 'revisions', titles: title,
			rvprop: 'content', rvslots: 'main', formatversion: 2, format: 'json'
		} ).then( function ( d ) {
			try {
				return d.query.pages[ 0 ].revisions[ 0 ].slots.main.content;
			} catch ( e ) {
				return '';
			}
		} );
	}

	function getRandom() {
		return fetch( API + '/random?limit=1', { headers: { Accept: 'application/json' } } )
			.then( function ( r ) { return r.json(); } )
			.then( function ( j ) {
				return ( j.pages && j.pages[ 0 ] ) ? j.pages[ 0 ].title : null;
			} );
	}

	// Full mainspace watchlist (titles only), paged.
	function fetchWatchlist() {
		var api = new mw.Api();
		var titles = [];
		function page( cont ) {
			var params = {
				action: 'query', list: 'watchlistraw', wrnamespace: 0,
				wrlimit: 'max', format: 'json', formatversion: 2
			};
			if ( cont ) { $.extend( params, cont ); }
			return api.get( params ).then( function ( d ) {
				( d.watchlistraw || [] ).forEach( function ( w ) {
					titles.push( w.title );
				} );
				if ( d.continue ) { return page( d.continue ); }
				return titles;
			} );
		}
		return page( null );
	}

	// Which of `titles` are in the worklist (chunked POST /pages, text/plain).
	function intersect( titles ) {
		var CHUNK = 500, chunks = [], i;
		for ( i = 0; i < titles.length; i += CHUNK ) {
			chunks.push( titles.slice( i, i + CHUNK ) );
		}
		var found = [];
		return chunks.reduce( function ( p, chunk ) {
			return p.then( function () {
				return fetch( API + '/pages', {
					method: 'POST',
					headers: { 'Content-Type': 'text/plain' },   // safelisted -> no preflight
					body: chunk.join( '\n' )
				} ).then( function ( r ) {
					return r.ok ? r.json() : { pages: [] };
				} ).then( function ( j ) {
					found = found.concat( j.pages || [] );
				} );
			} );
		}, Promise.resolve() ).then( function () { return found; } );
	}

	// ---- editing helpers --------------------------------------------------

	function applyAll( wikitext, cites ) {
		cites.forEach( function ( c ) {
			wikitext = wikitext.split( c.oldcite ).join( c.newcite );
		} );
		return wikitext;
	}

	function setSummary( text ) {
		var $sum = $( '#wpSummary, #wpSummaryWidget input' );
		if ( !$sum.length ) { return; }
		var cur = $sum.val() || '';
		if ( cur.indexOf( 'BooksUp' ) !== -1 ) { return; }
		$sum.val( cur ? cur + '; ' + text : text );
	}

	function esc( s ) {
		return $( '<div>' ).text( s == null ? '' : s ).html();
	}

	function renderNewcite( newcite, url ) {
		var html = esc( newcite );
		if ( url ) {
			var u = esc( url );
			if ( html.indexOf( u ) !== -1 ) {
				html = html.split( u ).join(
					'<a href="' + u + '" target="_blank" rel="noopener">' + u + '</a>' );
			}
		}
		return html;
	}

	// Navigate to a discovered article and auto-open the panel there.
	function goToArticle( title, inEdit ) {
		try {
			localStorage.setItem( AUTORUN_KEY,
				JSON.stringify( { page: title, ts: Date.now() } ) );
		} catch ( e ) {}
		var url = mw.util.getUrl( title );
		if ( inEdit ) { window.open( url, '_blank' ); }   // don't lose the open edit
		else { window.location.href = url; }
	}

	// ---- edit-page side: drop a stashed change into the edit form ---------

	function applyStashToEditor() {
		var raw = sessionStorage.getItem( STASH_KEY );
		if ( !raw ) { return; }
		sessionStorage.removeItem( STASH_KEY );

		var data;
		try { data = JSON.parse( raw ); } catch ( e ) { return; }
		if ( !data || data.page !== mw.config.get( 'wgPageName' ) ) { return; }

		var $text = $( '#wpTextbox1' );
		if ( !$text.length ) { return; }
		$text.val( data.text ).trigger( 'change' ).trigger( 'input' );
		setSummary( data.summary );

		mw.notify(
			'Applied ' + data.count + ' change' + ( data.count === 1 ? '' : 's' ) +
			'. Review the diff (Show changes) and save.',
			{ title: 'BooksUp', autoHide: false }
		);
	}

	// ---- panel ------------------------------------------------------------

	function injectStyle() {
		if ( document.getElementById( 'booksup-style' ) ) { return; }
		mw.util.addCSS(
			'#booksup-panel{position:fixed;top:80px;right:16px;z-index:1000;width:440px;' +
				'max-height:82vh;overflow:auto;background:#fff;border:1px solid #a2a9b1;' +
				'border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.2);font-size:13px;padding:0}' +
			'#booksup-panel h3{margin:0;padding:8px 12px;background:#36c;color:#fff;' +
				'font-size:14px;border-radius:4px 4px 0 0;display:flex;align-items:center;' +
				'gap:6px;cursor:move;user-select:none;position:sticky;top:0;z-index:2}' +
			'#booksup-panel h3 .bu-brand{flex:0 0 auto;font-weight:bold}' +
			'#booksup-panel h3 .bu-sep{flex:0 0 auto;opacity:.6}' +
			'#booksup-panel h3 .bu-title{flex:1 1 auto;min-width:0;overflow:hidden;' +
				'white-space:nowrap;text-overflow:ellipsis;font-weight:normal}' +
			'#booksup-panel h3 .bu-count{flex:0 0 auto;font-weight:normal;white-space:nowrap}' +
			'#booksup-panel h3 .bu-x{cursor:pointer;font-size:20px;line-height:1;padding:0 2px}' +
			'#booksup-panel h3 .bu-x:hover{color:#cfe2ff}' +
			'#booksup-panel .bu-section{padding:8px 12px;border-bottom:1px solid #eaecf0}' +
			'#booksup-panel .bu-h{font-weight:bold;margin:0 0 6px;color:#202122}' +
			'#booksup-panel .bu-none{color:#54595d;font-style:italic}' +
			'#booksup-panel ul{list-style:none;margin:0;padding:0}' +
			'#booksup-panel li{padding:8px 0;border-bottom:1px solid #eaecf0}' +
			'#booksup-panel li:last-child{border-bottom:0}' +
			'#booksup-panel li.bu-skipped{opacity:.45}' +
			'#booksup-panel .bu-lbl{font-weight:bold;font-size:11px;color:#54595d;margin:4px 0 2px}' +
			'#booksup-panel pre{white-space:pre-wrap;word-break:break-word;background:#f8f9fa;' +
				'border:1px solid #eaecf0;padding:4px;margin:0;font-size:11px}' +
			'#booksup-panel pre.new{background:#eaf3ff}' +
			'#booksup-panel pre.new a,#booksup-panel a.bu-ext{color:#3366cc;text-decoration:underline}' +
			'#booksup-panel .bu-toggle{margin:6px 0 2px}' +
			'#booksup-panel .bu-btn{margin-right:6px;padding:2px 12px;border:1px solid #a2a9b1;' +
				'background:#f8f9fa;color:#202122;cursor:pointer;border-radius:2px;font-size:12px}' +
			'#booksup-panel .bu-add.bu-on{background:#36c;border-color:#36c;color:#fff}' +
			'#booksup-panel .bu-skip.bu-on{background:#d33;border-color:#d33;color:#fff}' +
			'#booksup-panel .bu-actions button,#booksup-panel .bu-actions a{margin-right:8px}' +
			'#booksup-panel .bu-results{margin-top:6px}' +
			'#booksup-panel .bu-results li{padding:3px 0;border:0}' +
			'#booksup-panel .booksup-foot{position:sticky;bottom:0;background:#fff;' +
				'padding:8px 12px;border-top:1px solid #eaecf0;text-align:right}' +
			'#booksup-panel .booksup-foot button{margin-left:6px}'
		);
		$( '<span id="booksup-style">' ).appendTo( 'head' );
	}

	function closePanel() {
		$( '#booksup-panel' ).remove();
	}

	function makeDraggable( $panel, $handle ) {
		var sx, sy, startLeft, startTop;
		$handle.on( 'mousedown', function ( e ) {
			if ( $( e.target ).closest( '.bu-x' ).length ) { return; }
			var rect = $panel[ 0 ].getBoundingClientRect();
			$panel.css( { left: rect.left + 'px', top: rect.top + 'px', right: 'auto' } );
			sx = e.clientX; sy = e.clientY; startLeft = rect.left; startTop = rect.top;
			e.preventDefault();
			$( document ).on( 'mousemove.booksup', onMove ).on( 'mouseup.booksup', onUp );
		} );
		function onMove( e ) {
			var nl = Math.max( 0, Math.min( startLeft + ( e.clientX - sx ),
				window.innerWidth - 60 ) );
			var nt = Math.max( 0, Math.min( startTop + ( e.clientY - sy ),
				window.innerHeight - 30 ) );
			$panel.css( { left: nl + 'px', top: nt + 'px' } );
		}
		function onUp() {
			$( document ).off( 'mousemove.booksup mouseup.booksup' );
		}
	}

	function renderArticleSection( $section, cites ) {
		if ( !cites.length ) {
			$( '<div class="bu-none">' )
				.text( 'No book suggestions for this article.' ).appendTo( $section );
			return;
		}

		var $ul = $( '<ul>' ).appendTo( $section );
		cites.forEach( function ( c, i ) {
			var $li = $( '<li>' ).attr( 'data-i', i );
			$( '<div class="bu-lbl">' ).text( 'current' ).appendTo( $li );
			$( '<pre class="old">' ).text( c.oldcite ).appendTo( $li );

			var $plbl = $( '<div class="bu-lbl">' ).text( 'proposed ' ).appendTo( $li );
			$( '<a class="bu-ext" target="_blank" rel="noopener">' )
				.attr( 'href', c.url ).text( '↗ open link' ).appendTo( $plbl );
			$( '<pre class="new">' ).html( renderNewcite( c.newcite, c.url ) ).appendTo( $li );

			var $tog = $( '<div class="bu-toggle">' ).appendTo( $li );
			var $add = $( '<button class="bu-btn bu-add bu-on">' ).text( 'Add' );
			var $skip = $( '<button class="bu-btn bu-skip">' ).text( 'Skip' );
			$add.on( 'click', function () {
				$li.removeClass( 'bu-skipped' );
				$add.addClass( 'bu-on' ); $skip.removeClass( 'bu-on' );
			} );
			$skip.on( 'click', function () {
				$li.addClass( 'bu-skipped' );
				$skip.addClass( 'bu-on' ); $add.removeClass( 'bu-on' );
			} );
			$tog.append( $add, $skip );
			$li.appendTo( $ul );
		} );
	}

	function renderWatchlistResults( $results, found, inEdit ) {
		$results.empty();
		if ( !found.length ) {
			$( '<div class="bu-none">' )
				.text( 'None of your watchlisted articles have suggestions.' )
				.appendTo( $results );
			return;
		}
		found.sort( function ( a, b ) { return b.counts.total - a.counts.total; } );
		$( '<div class="bu-none">' ).text( found.length +
			' watchlisted article' + ( found.length === 1 ? '' : 's' ) +
			' with suggestions:' ).appendTo( $results );
		var $ul = $( '<ul>' ).appendTo( $results );
		found.forEach( function ( p ) {
			var $li = $( '<li>' );
			$( '<a href="#">' ).text( p.title + ' (' + p.counts.total + ')' )
				.on( 'click', function ( e ) {
					e.preventDefault();
					goToArticle( p.title, inEdit );
				} ).appendTo( $li );
			$li.appendTo( $ul );
		} );
	}

	function renderDiscoverSection( $section, inEdit ) {
		$( '<div class="bu-h">' ).text( 'Other articles with BooksUp suggestions available' ).appendTo( $section );
		var $actions = $( '<div class="bu-actions">' ).appendTo( $section );
		var $results = $( '<div class="bu-results">' ).appendTo( $section );

		$( '<button class="mw-ui-button">' ).text( 'Random' )
			.on( 'click', function () {
				mw.notify( 'Finding a random article…', { title: 'BooksUp', tag: 'booksup' } );
				getRandom().then( function ( title ) {
					if ( title ) { goToArticle( title, inEdit ); }
					else { mw.notify( 'No article found.', { title: 'BooksUp' } ); }
				} ).catch( function ( err ) {
					mw.notify( 'Error: ' + err.message, { title: 'BooksUp', type: 'error' } );
				} );
			} ).appendTo( $actions );

		$( '<button class="mw-ui-button">' ).text( 'My watchlist' )
			.on( 'click', function () {
				if ( !mw.config.get( 'wgUserName' ) ) {
					mw.notify( 'Log in to use your watchlist.', { title: 'BooksUp' } );
					return;
				}
				$results.html( '<div class="bu-none">Fetching your watchlist…</div>' );
				fetchWatchlist().then( function ( titles ) {
					$results.html( '<div class="bu-none">Checking ' + titles.length +
						' watchlisted article' + ( titles.length === 1 ? '' : 's' ) + '…</div>' );
					return intersect( titles );
				} ).then( function ( found ) {
					renderWatchlistResults( $results, found, inEdit );
				} ).catch( function ( err ) {
					$results.empty();
					mw.notify( 'Watchlist error: ' + err.message,
						{ title: 'BooksUp', type: 'error' } );
				} );
			} ).appendTo( $actions );

		$( '<a class="mw-ui-button" target="_blank" rel="noopener">' )
			.attr( 'href', WEB ).text( 'Browse worklist ↗' ).appendTo( $actions );
	}

	function showPanel( title, wikitext, cites, inEdit ) {
		injectStyle();
		closePanel();

		var $panel = $( '<div id="booksup-panel">' );
		var $h3 = $( '<h3>' ).appendTo( $panel );
		$( '<span class="bu-brand">' ).text( 'BooksUp' ).appendTo( $h3 );
		$( '<span class="bu-sep">' ).text( '|' ).appendTo( $h3 );
		$( '<span class="bu-title">' ).text( title.replace( /_/g, ' ' ) ).appendTo( $h3 );
		$( '<span class="bu-sep">' ).text( '|' ).appendTo( $h3 );
		$( '<span class="bu-count">' ).text( cites.length + ' book' +
			( cites.length === 1 ? '' : 's' ) ).appendTo( $h3 );
		$( '<span class="bu-x" title="Close">' ).text( '×' )
			.on( 'click', closePanel ).appendTo( $h3 );

		var $article = $( '<div class="bu-section">' ).appendTo( $panel );
		renderArticleSection( $article, cites );

		var $discover = $( '<div class="bu-section">' ).appendTo( $panel );
		renderDiscoverSection( $discover, inEdit );

		var $foot = $( '<div class="booksup-foot">' ).appendTo( $panel );
		$( '<button class="mw-ui-button">' ).text( 'Close' )
			.on( 'click', closePanel ).appendTo( $foot );

		if ( cites.length ) {
			$( '<button class="mw-ui-button mw-ui-progressive">' )
				.text( inEdit ? 'Apply to editor' : 'Open in editor' )
				.on( 'click', function () {
					var chosen = [];
					$panel.find( '.bu-section li' ).not( '.bu-skipped' ).each( function () {
						var idx = $( this ).attr( 'data-i' );
						if ( idx !== undefined ) { chosen.push( cites[ idx ] ); }
					} );
					if ( !chosen.length ) {
						mw.notify( 'Nothing to add (all skipped).', { title: 'BooksUp' } );
						return;
					}
					if ( inEdit ) {
						var $t = $( '#wpTextbox1' );
						$t.val( applyAll( $t.val() || '', chosen ) )
							.trigger( 'change' ).trigger( 'input' );
						setSummary( editSummary( chosen.length ) );
						closePanel();
						mw.notify( 'Applied ' + chosen.length + ' change' +
							( chosen.length === 1 ? '' : 's' ) +
							' to the editor. Review and save.', { title: 'BooksUp' } );
					} else {
						sessionStorage.setItem( STASH_KEY, JSON.stringify( {
							page: title, text: applyAll( wikitext, chosen ),
							summary: editSummary( chosen.length ), count: chosen.length
						} ) );
						window.location.href = mw.util.getUrl( title, { action: 'edit' } );
					}
				} ).appendTo( $foot );
		}

		$( document.body ).append( $panel );
		makeDraggable( $panel, $h3 );
	}

	function run( inEdit ) {
		var title = mw.config.get( 'wgPageName' );
		mw.notify( 'Checking…', { title: 'BooksUp', tag: 'booksup' } );

		var wikitextP = inEdit ?
			Promise.resolve( $( '#wpTextbox1' ).val() || '' ) :
			fetchWikitext( title );

		Promise.all( [ fetchCandidates( title ), wikitextP ] )
			.then( function ( res ) {
				var cites = ( res[ 0 ] && res[ 0 ].citations ) || [];
				var wikitext = res[ 1 ] || '';
				var applicable = cites.filter( function ( c ) {
					return c.oldcite && wikitext.indexOf( c.oldcite ) !== -1;
				} );
				showPanel( title, wikitext, applicable, inEdit );
			} )
			.catch( function ( err ) {
				mw.notify( 'Error: ' + err.message,
					{ title: 'BooksUp', type: 'error', tag: 'booksup' } );
			} );
	}

	function maybeAutorun() {
		var raw;
		try { raw = localStorage.getItem( AUTORUN_KEY ); } catch ( e ) { return; }
		if ( !raw ) { return; }
		var d;
		try { d = JSON.parse( raw ); } catch ( e ) {
			localStorage.removeItem( AUTORUN_KEY ); return;
		}
		if ( !d || !d.page ) { localStorage.removeItem( AUTORUN_KEY ); return; }
		var here = mw.config.get( 'wgPageName' ).replace( /_/g, ' ' );
		if ( d.page.replace( /_/g, ' ' ) !== here ) { return; }   // not this page
		if ( Date.now() - ( d.ts || 0 ) > 60000 ) {
			localStorage.removeItem( AUTORUN_KEY ); return;       // stale
		}
		localStorage.removeItem( AUTORUN_KEY );
		run( false );
	}

	function addLink( inEdit ) {
		var link = mw.util.addPortletLink(
			'p-tb', '#', 'BooksUp', 't-booksup', 'Find book links for this article'
		);
		if ( link ) {
			$( link ).on( 'click', function ( e ) {
				e.preventDefault();
				run( inEdit );
			} );
		}
	}

	// ---- init -------------------------------------------------------------

	var action = mw.config.get( 'wgAction' );
	var ns = mw.config.get( 'wgNamespaceNumber' );

	if ( action === 'edit' || action === 'submit' ) {
		mw.hook( 'wikipage.editform' ).add( applyStashToEditor );
		if ( ns === 0 ) {
			mw.loader.using( [ 'mediawiki.util' ] ).then( function () {
				addLink( true );
			} );
		}
		return;
	}

	if ( ns === 0 && action === 'view' ) {
		mw.loader.using( [ 'mediawiki.util', 'mediawiki.api' ] ).then( function () {
			addLink( false );
			maybeAutorun();
		} );
	}
}() );
